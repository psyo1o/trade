# -*- coding: utf-8 -*-
"""
장부(positions) + 시세로 예수·총평·보유 목록 추정 — KIS 잔고 API 없이 GUI·매도 루프·Phase5 보조.

봇만 매매할 때 HTS/MTS 실보유와 장부가 일치한다는 전제.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from utils.helpers import is_coin_ticker, normalize_ticker

_LEGACY_CASH_KEYS = {"KR": "last_kr_cash_krw", "US": "last_us_cash_usd"}
_BUY_CASH_GUARD_KEY = "_buy_cash_guard"
# 미장 매수 직후 예수 API 지연·재오염은 당일~익일 세션까지 이어질 수 있음
_BUY_CASH_GUARD_TTL_SEC = 24 * 3600.0


def _market_norm(market: str) -> str:
    return str(market or "").strip().upper()


def _kis_snap_bucket(state: dict, market: str) -> dict:
    snap = state.get("last_kis_display_snapshot")
    if not isinstance(snap, dict):
        return {}
    key = "kr" if _market_norm(market) == "KR" else "us"
    part = snap.get(key)
    return part if isinstance(part, dict) else {}


def kis_display_total(state: dict, market: str) -> float:
    """국·미 총평 — KIS 스냅샷. 장부+시세 루프는 ``_phase5_aux_sync`` 추정값 우선."""
    m = _market_norm(market)
    aux = state.get("_phase5_aux_sync")
    if isinstance(aux, dict) and aux.get("ledger_only"):
        key = "kr_krw" if m == "KR" else "usd_total"
        raw = aux.get(key)
        if raw is not None:
            v = float(raw or 0)
            if v > 0:
                return v
    part = _kis_snap_bucket(state, market)
    t = float(part.get("total", 0) or 0)
    if t > 0:
        return t
    m = _market_norm(market)
    if m == "KR":
        return float(state.get("circuit_aux_last_kr_krw", 0) or state.get("last_kr_cash_krw", 0) or 0)
    return float(state.get("circuit_aux_last_usd_total", 0) or state.get("last_us_cash_usd", 0) or 0)


def _cash_looks_like_total_as_cash(cash: float, total: float) -> bool:
    return cash > 0 and total > 0 and cash >= total * 0.95


def display_cash_from_state(
    state: dict,
    market: str,
    snap_part: dict | None = None,
) -> float:
    """예수 — ``last_kis_display_snapshot`` 만 (옛 키는 스냅샷 비었을 때만 읽기)."""
    m = _market_norm(market)
    part = snap_part if isinstance(snap_part, dict) else _kis_snap_bucket(state, m)
    snap_cash = float(part.get("cash", 0) or 0)
    snap_total = float(part.get("total", 0) or 0)
    stale = _cash_looks_like_total_as_cash(snap_cash, snap_total)
    if snap_cash > 0 and not stale:
        return snap_cash
    if stale:
        legacy = float(state.get(_LEGACY_CASH_KEYS.get(m, ""), 0) or 0)
        if legacy > 0 and legacy < snap_cash * 0.9:
            return legacy
        return 0.0
    if not part:
        legacy = float(state.get(_LEGACY_CASH_KEYS.get(m, ""), 0) or 0)
        if legacy > 0:
            return legacy
    return snap_cash


def write_kis_display_snapshot_part(
    state: dict,
    market: str,
    *,
    cash: float,
    total: float,
    roi: Any = None,
    saved_at: str | None = None,
    force: bool = False,
) -> None:
    """국·미 예수·총평 — ``last_kis_display_snapshot`` 에만 저장 (단일 저장소)."""
    m = _market_norm(market)
    if m not in ("KR", "US"):
        return
    snap = state.get("last_kis_display_snapshot")
    if not isinstance(snap, dict):
        snap = {}
    bucket_key = "kr" if m == "KR" else "us"
    prev = snap.get(bucket_key)
    entry: dict[str, Any] = dict(prev) if isinstance(prev, dict) else {}
    nc = float(cash)
    nt = float(total)
    if not force and isinstance(prev, dict):
        pc = float(prev.get("cash", 0) or 0)
        pt = float(prev.get("total", 0) or 0)
        min_pt = 10_000.0 if m == "KR" else 50.0
        if (
            pt >= min_pt
            and pc > 0
            and nc > 0
            and nc < pc * 0.55
            and nt < pt * 0.85
        ):
            print(
                f"  📌 [snapshot {m}] 잔고 API 예수·총평 급감(일시/점검 추정) — "
                f"저장 스냅샷 유지 (new cash={nc}, total={nt} / prev cash={pc}, total={pt})"
            )
            return
    entry["cash"] = int(nc) if m == "KR" else float(nc)
    entry["total"] = int(nt) if m == "KR" else float(nt)
    if roi is not None:
        entry["roi"] = roi
    snap[bucket_key] = entry
    if saved_at:
        snap["saved_at"] = saved_at
    elif not snap.get("saved_at"):
        snap["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["last_kis_display_snapshot"] = snap


def _ledger_qty(pos: dict | None, fallback: float = 0.0) -> float:
    if not isinstance(pos, dict):
        return float(fallback)
    try:
        q = float(pos.get("qty", 0) or 0)
    except (TypeError, ValueError):
        q = 0.0
    return q if q > 0 else float(fallback)


def held_kr_codes_from_ledger(state: dict) -> list[str]:
    out: list[str] = []
    for t in (state.get("positions") or {}):
        c = str(t).strip()
        if c.isdigit() and len(c) == 6:
            out.append(normalize_ticker(c))
    return sorted(set(out))


def held_us_codes_from_ledger(state: dict) -> list[str]:
    out: list[str] = []
    for t in (state.get("positions") or {}):
        c = normalize_ticker(str(t))
        if c and not c.isdigit() and not is_coin_ticker(c):
            out.append(c)
    return sorted(set(out))


def _kr_row_from_position(code: str, pos: dict, curr_p: float) -> dict[str, Any]:
    qty = _ledger_qty(pos, 1.0)
    buy_p = float(pos.get("buy_p", 0) or 0)
    return {
        "pdno": code,
        "hldg_qty": str(int(qty)),
        "ccld_qty_smtl1": str(int(qty)),
        "pchs_avg_prc": str(buy_p),
        "pchs_avg_pric": str(buy_p),
        "prpr": str(int(curr_p)),
        "stck_prpr": str(int(curr_p)),
    }


def synthetic_kr_balance_dict(
    state: dict,
    *,
    resolve_kr_price: Callable[[str, dict, float], float],
) -> dict[str, Any]:
    """KIS 잔고 응답 형태 — ``output1``/``output2`` (장부·시세 기반)."""
    positions = state.get("positions") or {}
    holdings_value = 0.0
    output1: list[dict] = []
    for code, pos in positions.items():
        if not str(code).isdigit():
            continue
        if not isinstance(pos, dict):
            continue
        buy_p = float(pos.get("buy_p", 0) or 0)
        if buy_p <= 0:
            continue
        c = normalize_ticker(str(code))
        curr_p = float(resolve_kr_price(c, pos, buy_p))
        qty = _ledger_qty(pos, 1.0)
        holdings_value += qty * curr_p
        output1.append(_kr_row_from_position(c, pos, curr_p))

    cash = display_cash_from_state(state, "KR")
    total = cash + holdings_value
    snap_total = kis_display_total(state, "KR")
    if holdings_value <= 0 and snap_total > cash * 1.02:
        cash = snap_total
        total = snap_total
    elif total <= 0 and snap_total > 0:
        total = snap_total
        if cash <= 0:
            cash = max(0.0, total - holdings_value)

    output2 = [
        {
            "prvs_rcdl_excc_amt": str(int(cash)),
            "tot_evlu_amt": str(int(total)),
        }
    ]
    return {"rt_cd": "0", "msg1": "ledger_valuation", "output1": output1, "output2": output2}


def synthetic_us_balance_dict(
    state: dict,
    *,
    resolve_us_price: Callable[[str, dict, float], float],
) -> dict[str, Any]:
    positions = state.get("positions") or {}
    holdings_value = 0.0
    output1: list[dict] = []
    for raw, pos in positions.items():
        t = normalize_ticker(str(raw))
        if not t or t.isdigit() or is_coin_ticker(t):
            continue
        if not isinstance(pos, dict):
            continue
        buy_p = float(pos.get("buy_p", 0) or 0)
        if buy_p <= 0:
            continue
        curr_p = float(resolve_us_price(t, pos, buy_p))
        qty = _ledger_qty(pos, 1.0)
        holdings_value += qty * curr_p
        output1.append(
            {
                "ovrs_pdno": t,
                "ovrs_cblc_qty": str(qty),
                "ovrs_avg_unpr": str(buy_p),
                "ovrs_now_prc2": str(curr_p),
            }
        )

    cash = display_cash_from_state(state, "US")
    total = cash + holdings_value
    snap_total = kis_display_total(state, "US")
    if holdings_value <= 0 and snap_total > cash * 1.02:
        cash = snap_total
        total = snap_total
    elif total <= 0 and snap_total > 0:
        total = snap_total
        if cash <= 0:
            cash = max(0.0, total - holdings_value)

    output2 = {"ovrs_stck_evlu_amt": str(holdings_value), "frcr_dncl_amt_2": str(cash)}
    return {"rt_cd": "0", "msg1": "ledger_valuation", "output1": output1, "output2": output2}


def _ensure_kis_snap_part(state: dict, market: str) -> dict:
    """``last_kis_display_snapshot`` 버킷을 보장하고 그 dict 를 반환."""
    m = _market_norm(market)
    snap = state.get("last_kis_display_snapshot")
    if not isinstance(snap, dict):
        snap = {}
        state["last_kis_display_snapshot"] = snap
    key = "kr" if m == "KR" else "us"
    part = snap.get(key)
    if not isinstance(part, dict):
        part = {}
        snap[key] = part
    return part


def _set_buy_cash_guard(state: dict, market: str, cash: float, total: float) -> None:
    part = _ensure_kis_snap_part(state, market)
    part[_BUY_CASH_GUARD_KEY] = {
        "cash": float(cash),
        "total": float(total),
        "ts": float(time.time()),
    }


def _get_buy_cash_guard(part: dict) -> dict | None:
    raw = part.get(_BUY_CASH_GUARD_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        ts = float(raw.get("ts", 0) or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0 or (time.time() - ts) > _BUY_CASH_GUARD_TTL_SEC:
        return None
    return raw


def _recent_equity_buy_spend(
    state: dict,
    market: str,
    *,
    max_age_sec: float = _BUY_CASH_GUARD_TTL_SEC,
) -> float:
    """최근 매수 포지션의 매입대금 합(국장=원, 미장=USD)."""
    m = _market_norm(market)
    positions = state.get("positions")
    if not isinstance(positions, dict) or not positions:
        return 0.0
    now = time.time()
    spent = 0.0
    for code, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        ticker = normalize_ticker(code)
        if not ticker or is_coin_ticker(ticker):
            continue
        is_kr = ticker.isdigit()
        if m == "KR" and not is_kr:
            continue
        if m == "US" and is_kr:
            continue
        try:
            bt = float(pos.get("buy_time") or 0)
        except (TypeError, ValueError):
            bt = 0.0
        if bt <= 0 or (now - bt) > max_age_sec:
            continue
        try:
            qty = float(pos.get("qty") or 0)
            buy_p = float(pos.get("buy_p") or 0)
        except (TypeError, ValueError):
            continue
        if qty > 0 and buy_p > 0:
            spent += qty * buy_p
    return float(spent)


def _sanitize_kis_cash_total_persist(
    market: str,
    state: dict,
    cash: float,
    stock: float,
) -> tuple[float, float]:
    """매수·매도 직후 KIS API 지연/이중합산을 저장 전에 보정."""
    m = _market_norm(market)
    part = _kis_snap_bucket(state, m)
    prev_cash = float(part.get("cash", 0) or 0)
    prev_total = float(part.get("total", 0) or 0)
    min_pt = 10_000.0 if m == "KR" else 50.0
    min_sold = 25_000.0 if m == "KR" else 25.0
    nc = float(cash)
    stock_v = float(stock)
    total = nc + stock_v
    if prev_total < min_pt:
        return nc, total
    prev_stock = max(0.0, prev_total - prev_cash)
    if stock_v <= 0:
        return nc, total
    stock_grew = prev_stock > 0 and stock_v > prev_stock * 1.08
    cash_stale = nc >= prev_cash * 0.85
    total_inflated = total > prev_total * 1.03
    if stock_grew and cash_stale and total_inflated:
        nc = max(0.0, prev_total - stock_v)
        total = nc + stock_v
        _set_buy_cash_guard(state, m, nc, total)
        print(
            f"  📌 [snapshot {m}] persist 예수·총평 이중합산 추정 — "
            f"예수 역산 {nc:,.2f}{'원' if m == 'KR' else ''} · 총평 {total:,.2f}"
        )
        return nc, total

    # Case B2: 1차 역산 직후 API 예수가 다시 매수 전 값으로 튀는 재오염 차단
    guard = _get_buy_cash_guard(part)
    if guard is not None:
        g_cash = float(guard.get("cash", 0) or 0)
        g_total = float(guard.get("total", 0) or 0)
        if (
            g_total >= min_pt
            and nc > max(g_cash * 1.5, g_cash + min_sold)
            and (nc + stock_v) > g_total * 1.03
            and stock_v >= max(0.0, g_total - g_cash) * 0.92
        ):
            g_stock = max(0.0, g_total - g_cash)
            if abs(stock_v - g_stock) <= max(min_sold, g_stock * 0.08):
                nc = g_cash
            else:
                nc = max(0.0, g_total - stock_v)
            total = nc + stock_v
            print(
                f"  📌 [snapshot {m}] persist 이중합산 재오염 차단 — "
                f"예수 {nc:,.2f}{'원' if m == 'KR' else ''} · 총평 {total:,.2f}"
            )
            return nc, total
        # API 예수가 가드와 맞게 내려오면 해제
        if g_cash > 0 and nc <= g_cash * 1.25 and not total_inflated:
            live_part = _ensure_kis_snap_part(state, m)
            live_part.pop(_BUY_CASH_GUARD_KEY, None)

    # Case B3: 최근 매입대금이 예수에 그대로 남아 잔여만 남는 형태(입금과 구분)
    recent_spend = _recent_equity_buy_spend(state, m)
    if recent_spend >= min_sold and nc >= recent_spend * 0.85 and stock_v > 0:
        residual = nc - recent_spend
        residual_cap = max(min_sold * 2.0, recent_spend * 0.08)
        if 0.0 <= residual <= residual_cap:
            adj_cash = residual
            adj_total = adj_cash + stock_v
            if adj_total < total * 0.97:
                _set_buy_cash_guard(state, m, adj_cash, adj_total)
                print(
                    f"  📌 [snapshot {m}] persist 최근매수 예수 미반영 보정 — "
                    f"매입≈{recent_spend:,.2f} → 예수 {adj_cash:,.2f}"
                    f"{'원' if m == 'KR' else ''} · 총평 {adj_total:,.2f}"
                )
                return adj_cash, adj_total

    # Case B2b: 가드 없이 이미 저예수 스냅샷인데 API만 옛 예수로 복귀
    if (
        prev_total >= min_pt
        and prev_cash > 0
        and prev_cash < prev_total * 0.25
        and nc > max(prev_cash * 2.0, prev_cash + min_sold)
        and stock_v >= prev_stock * 0.92
        and total_inflated
    ):
        if abs(stock_v - prev_stock) <= max(min_sold, prev_stock * 0.08):
            nc = prev_cash
        else:
            nc = max(0.0, prev_total - stock_v)
        total = nc + stock_v
        _set_buy_cash_guard(state, m, nc, total)
        print(
            f"  📌 [snapshot {m}] persist 이중합산 재오염 차단 — "
            f"예수 {nc:,.2f}{'원' if m == 'KR' else ''} · 총평 {total:,.2f}"
        )
        return nc, total

    stock_sold = max(0.0, prev_stock - stock_v)
    if stock_sold >= min_sold and total < prev_total * 0.97 and nc < prev_cash + stock_sold * 0.5:
        if m == "US":
            from run_bot import _reconcile_us_equity_after_sell

            nc, total = _reconcile_us_equity_after_sell(
                prev_cash=prev_cash,
                prev_total=prev_total,
                prev_stock=prev_stock,
                cash=nc,
                stock=stock_v,
            )
        else:
            nc = prev_cash + stock_sold
            total = nc + stock_v
            print(
                f"  📌 [snapshot {m}] persist 매도 정산 지연 추정 — "
                f"예수 {nc:,.0f}원 · 총평 {total:,.0f}원"
            )
    elif total < prev_total * 0.97:
        stock_stable = prev_stock > 0 and abs(stock_v - prev_stock) < max(min_sold, prev_stock * 0.08)
        cash_dropped = prev_cash > 0 and nc < prev_cash * 0.85
        if stock_stable and cash_dropped:
            if m == "US":
                from run_bot import _reconcile_us_equity_after_sell

                nc, total = _reconcile_us_equity_after_sell(
                    prev_cash=prev_cash,
                    prev_total=prev_total,
                    prev_stock=prev_stock,
                    cash=nc,
                    stock=stock_v,
                )
            else:
                nc = prev_total - stock_v
                total = prev_total
                print(
                    f"  📌 [snapshot {m}] persist 정산 지연 지속 추정 — "
                    f"예수 {nc:,.0f}원 · 총평 {total:,.0f}원"
                )
    return nc, total


def coalesce_ledger_kis_labels(
    market: str,
    state: dict,
    kis_snap_part: dict | None,
    holdings_current: float,
    *,
    cash_guess: float = 0.0,
    total_guess: float = 0.0,
) -> tuple[float, float]:
    """장부+시세 GUI 라벨 — 예수에 총평이 섞여 있으면 ``cash+보유`` 이중 합산을 막는다.

  * 예수: ``display_cash_from_state`` (``last_kis_display_snapshot`` 단일 소스)
  * 총평: 정리된 예수 + 장부 보유 평가(표시 시세)
    """
    part = kis_snap_part if isinstance(kis_snap_part, dict) else {}
    snap_total = float(part.get("total", 0) or 0)
    snap_cash_raw = float(part.get("cash", 0) or 0)
    hc = float(holdings_current or 0.0)
    m = _market_norm(market)
    cash = display_cash_from_state(state, m, part)
    if cash <= 0:
        cash = float(cash_guess or 0.0)

    ref_total = snap_total if snap_total > 0 else float(total_guess or 0.0)
    min_sold = 25_000.0 if m == "KR" else 25.0
    if hc <= 0 and ref_total > max(cash, 0.0) * 1.02:
        cash = ref_total
        total = ref_total
    elif hc > 0 and ref_total > 0:
        prev_implied_stock = max(0.0, snap_total - snap_cash_raw) if snap_total > 0 else 0.0
        stock_sold = max(0.0, prev_implied_stock - hc)
        live_total = float(cash) + hc
        if (
            prev_implied_stock > 0
            and hc > prev_implied_stock * 1.08
            and cash >= snap_cash_raw * 0.85
        ):
            cash = max(0.0, ref_total - hc)
        elif (
            stock_sold >= min_sold
            and ref_total > live_total * 1.02
            and cash < snap_cash_raw + stock_sold * 0.5
        ):
            cash = max(0.0, snap_cash_raw + stock_sold)
        elif cash >= ref_total * 0.95:
            cash = max(0.0, ref_total - hc)
        elif cash + hc > ref_total * 1.05:
            cash = max(0.0, ref_total - hc)

    total = float(cash) + hc
    if total <= 0 and ref_total > 0:
        total = ref_total
        if hc > 0 and cash <= 0:
            cash = max(0.0, ref_total - hc)

    if m == "KR":
        return float(int(round(cash))), float(int(round(total)))
    return float(cash), float(total)


def persist_kr_cash_from_balance(bal: dict, state: dict) -> None:
    """KIS 국장 잔고 조회 성공 시 ``last_kis_display_snapshot.kr`` 갱신."""
    if not isinstance(bal, dict):
        return
    try:
        from api.kis_parsers import kis_response_rate_limited, parse_kr_cash_total
        from run_bot import _to_float

        if kis_response_rate_limited(bal):
            return
        cash, total_parsed = parse_kr_cash_total(bal.get("output2", []), _to_float)
        from run_bot import _calc_kr_holdings_metrics

        stock = float(_calc_kr_holdings_metrics(bal).get("current", 0.0) or 0.0)
        if stock <= 0 and total_parsed > cash:
            stock = max(0.0, float(total_parsed) - float(cash))
        cash, total = _sanitize_kis_cash_total_persist("KR", state, float(cash), stock)
        if cash > 0 or total > 0:
            write_kis_display_snapshot_part(
                state, "KR", cash=float(cash), total=float(total), force=True
            )
    except Exception:
        pass


def persist_us_cash_from_balance(bal: dict, state: dict) -> None:
    """KIS 미장 잔고 조회 성공 시 ``last_kis_display_snapshot.us`` 갱신."""
    if not isinstance(bal, dict):
        return
    try:
        from api.kis_parsers import (
            kis_response_rate_limited,
            parse_us_cash_fallback,
        )
        from run_bot import _to_float

        if kis_response_rate_limited(bal):
            return
        from run_bot import (
            _compute_us_stock_value_from_output,
            _recover_us_cash_from_output2_if_needed,
            _resolve_us_display_cash,
            get_us_cash_real,
            kis_api,
        )

        out2 = bal.get("output2", {})
        cash = _resolve_us_display_cash(kis_api.broker_us, out2, refresh=False)
        if cash <= 0:
            cash = float(get_us_cash_real(kis_api.broker_us) or 0.0)
            cash = float(_recover_us_cash_from_output2_if_needed(cash, out2))
        stock = float(_compute_us_stock_value_from_output(bal, out2))
        cash, total = _sanitize_kis_cash_total_persist("US", state, cash, stock)
        if cash > 0 or total > 0:
            write_kis_display_snapshot_part(
                state, "US", cash=cash, total=total, force=True
            )
    except Exception:
        pass


def update_circuit_aux_from_ledger(
    state: dict,
    *,
    resolve_kr_price: Callable[[str, dict, float], float],
    resolve_us_price: Callable[[str, dict, float], float],
    estimate_usdkrw: Callable[[], float],
    coin_equity_krw: float | None = None,
) -> dict[str, Any]:
    """Phase5·표시용 ``circuit_aux_last_*`` — KIS 없이 장부+시세로 갱신.

    매수 후 예수 미반영(스냅샷 예수 높음+장부 보유 증가)이면 ``coalesce`` 로 역산한다.
    """
    kr_bal = synthetic_kr_balance_dict(state, resolve_kr_price=resolve_kr_price)
    us_bal = synthetic_us_balance_dict(state, resolve_us_price=resolve_us_price)

    try:
        from run_bot import _calc_kr_holdings_metrics, _calc_us_holdings_metrics

        kr_h = float(_calc_kr_holdings_metrics(kr_bal).get("current", 0.0) or 0.0)
        us_h = float(_calc_us_holdings_metrics(us_bal).get("current", 0.0) or 0.0)
    except Exception:
        kr_h = 0.0
        us_h = 0.0

    snap = state.get("last_kis_display_snapshot") if isinstance(state.get("last_kis_display_snapshot"), dict) else {}
    kr_part = snap.get("kr") if isinstance(snap.get("kr"), dict) else {}
    us_part = snap.get("us") if isinstance(snap.get("us"), dict) else {}
    kr_cash, kr_total = coalesce_ledger_kis_labels(
        "KR", state, kr_part, kr_h, cash_guess=float(kr_part.get("cash", 0) or 0),
        total_guess=float(kr_part.get("total", 0) or 0),
    )
    us_cash, us_total = coalesce_ledger_kis_labels(
        "US", state, us_part, us_h, cash_guess=float(us_part.get("cash", 0) or 0),
        total_guess=float(us_part.get("total", 0) or 0),
    )

    coin_k = (
        float(coin_equity_krw)
        if coin_equity_krw is not None
        else float(state.get("circuit_aux_last_coin_krw", 0) or 0)
    )

    state["circuit_aux_last_coin_krw"] = float(coin_k)

    rate = float(estimate_usdkrw())
    return {
        "kr_ok": True,
        "us_ok": True,
        "coin_ok": coin_k > 0 or bool(state.get("circuit_aux_last_coin_krw")),
        "weekend_kis_skip": False,
        "ledger_only": True,
        "totals": {
            "kr_krw": float(kr_total),
            "usd_total": float(us_total),
            "coin_krw": float(coin_k),
            "total_krw_est": float(kr_total) + float(coin_k) + float(us_total) * rate,
        },
    }
