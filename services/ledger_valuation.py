# -*- coding: utf-8 -*-
"""
장부(positions) + 시세로 예수·총평·보유 목록 추정 — KIS 잔고 API 없이 GUI·매도 루프·Phase5 보조.

봇만 매매할 때 HTS/MTS 실보유와 장부가 일치한다는 전제.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from utils.helpers import is_coin_ticker, normalize_ticker

_LEGACY_CASH_KEYS = {"KR": "last_kr_cash_krw", "US": "last_us_cash_usd"}


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
    hc = float(holdings_current or 0.0)
    m = _market_norm(market)
    cash = display_cash_from_state(state, m, part)
    if cash <= 0:
        cash = float(cash_guess or 0.0)

    ref_total = snap_total if snap_total > 0 else float(total_guess or 0.0)
    if hc <= 0 and ref_total > max(cash, 0.0) * 1.02:
        cash = ref_total
        total = ref_total
    elif hc > 0 and ref_total > 0:
        if cash >= ref_total * 0.95:
            cash = max(0.0, ref_total - hc)
        elif cash + hc > ref_total * 1.12:
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
        cash, total = parse_kr_cash_total(bal.get("output2", []), _to_float)
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
            compute_us_stock_value_from_output,
            kis_response_rate_limited,
            parse_us_cash_fallback,
        )
        from run_bot import _to_float

        if kis_response_rate_limited(bal):
            return
        out2 = bal.get("output2", {})
        cash = float(parse_us_cash_fallback(out2, _to_float))
        stock = float(compute_us_stock_value_from_output(bal, out2, _to_float))
        total = cash + stock
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
    """Phase5·표시용 ``circuit_aux_last_*`` — KIS 없이 장부+시세로 갱신."""
    kr_bal = synthetic_kr_balance_dict(state, resolve_kr_price=resolve_kr_price)
    us_bal = synthetic_us_balance_dict(state, resolve_us_price=resolve_us_price)
    from api.kis_parsers import parse_kr_cash_total

    try:
        from run_bot import _to_float

        _, kr_total = parse_kr_cash_total(kr_bal.get("output2", []), _to_float)
    except Exception:
        kr_total = kis_display_total(state, "KR")

    us_total = kis_display_total(state, "US")
    try:
        out2 = us_bal.get("output2", {})
        if isinstance(out2, dict):
            cash_u = float(out2.get("frcr_dncl_amt_2", 0) or 0)
            stock_u = float(out2.get("ovrs_stck_evlu_amt", 0) or 0)
            est = cash_u + stock_u
            if est > 0:
                us_total = est
    except Exception:
        pass

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
