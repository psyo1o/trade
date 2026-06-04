# -*- coding: utf-8 -*-
"""
장부(positions) + 시세로 예수·총평·보유 목록 추정 — KIS 잔고 API 없이 GUI·매도 루프·Phase5 보조.

봇만 매매할 때 HTS/MTS 실보유와 장부가 일치한다는 전제.
"""
from __future__ import annotations

from typing import Any, Callable

from utils.helpers import is_coin_ticker, normalize_ticker


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

    cash = float(state.get("last_kr_cash_krw", 0) or 0)
    total = cash + holdings_value
    if total <= 0 and float(state.get("circuit_aux_last_kr_krw", 0) or 0) > 0:
        total = float(state["circuit_aux_last_kr_krw"])
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

    cash = float(state.get("last_us_cash_usd", 0) or 0)
    total = cash + holdings_value
    if total <= 0 and float(state.get("circuit_aux_last_usd_total", 0) or 0) > 0:
        total = float(state["circuit_aux_last_usd_total"])
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

  * 예수: ``last_kis_display_snapshot`` → ``last_*_cash_*`` → synthetic 파싱 순
  * 총평: 정리된 예수 + 장부 보유 평가(표시 시세)
  """
    part = kis_snap_part if isinstance(kis_snap_part, dict) else {}
    snap_cash = float(part.get("cash", 0) or 0)
    snap_total = float(part.get("total", 0) or 0)
    hc = float(holdings_current or 0.0)
    m = str(market or "").strip().upper()
    state_key = "last_kr_cash_krw" if m == "KR" else "last_us_cash_usd"
    state_cash = float(state.get(state_key, 0) or 0.0)

    cash = snap_cash if snap_cash > 0 else state_cash
    if cash <= 0:
        cash = float(cash_guess or 0.0)

    ref_total = snap_total if snap_total > 0 else float(total_guess or 0.0)
    if hc > 0 and ref_total > 0:
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
    from api.kis_parsers import parse_kr_cash_total

    try:
        from run_bot import _to_float

        out2 = bal.get("output2", [])
        cash, total = parse_kr_cash_total(out2, _to_float)
        state["last_kr_cash_krw"] = float(cash)
        state["circuit_aux_last_kr_krw"] = float(total)
    except Exception:
        pass


def persist_us_cash_from_balance(bal: dict, state: dict) -> None:
    try:
        from run_bot import _to_float, safe_get
        from api.kis_parsers import parse_us_cash_fallback

        out2 = safe_get(bal, "output2", {})
        cash = float(_to_float(parse_us_cash_fallback(out2, _to_float), 0.0))
        rows = bal.get("output1", []) if isinstance(bal.get("output1"), list) else []
        stock_v = 0.0
        for s in rows:
            q = float(_to_float(s.get("ovrs_cblc_qty", 0)))
            p = float(_to_float(s.get("ovrs_now_prc2", s.get("ovrs_avg_unpr", 0))))
            stock_v += q * p
        state["last_us_cash_usd"] = cash
        state["circuit_aux_last_usd_total"] = float(cash + stock_v)
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
        kr_total = float(state.get("circuit_aux_last_kr_krw", 0) or 0)

    us_total = float(state.get("circuit_aux_last_usd_total", 0) or 0)
    try:
        out2 = us_bal.get("output2", {})
        if isinstance(out2, dict):
            cash_u = float(out2.get("frcr_dncl_amt_2", 0) or 0)
            stock_u = float(out2.get("ovrs_stck_evlu_amt", 0) or 0)
            us_total = cash_u + stock_u
    except Exception:
        pass

    coin_k = (
        float(coin_equity_krw)
        if coin_equity_krw is not None
        else float(state.get("circuit_aux_last_coin_krw", 0) or 0)
    )

    state["circuit_aux_last_kr_krw"] = float(kr_total)
    state["circuit_aux_last_usd_total"] = float(us_total)
    state["circuit_aux_last_coin_krw"] = float(coin_k)
    persist_kr_cash_from_balance(kr_bal, state)
    persist_us_cash_from_balance(us_bal, state)

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
