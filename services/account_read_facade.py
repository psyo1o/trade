"""
account_read_facade — 국·미 보유 조회의 **단일 진입점**.

역할
    * KIS ``output1`` 기반 실보유 목록·수량·(가능 시) 현재가를 조합한다.
    * **주말/점검** 구간에서는 API를 부르지 않고 ``bot_state.json`` 의 ``positions`` 로 폴백한다.

로그 정책
    * API 실패·필드 누락은 ``❌`` 로 즉시 출력(기존 유지).
    * 장부 폴백·빈 응답도 **조용히 빈 리스트를 반환하지 않고** 이유를 한 줄 남긴다(2026-04-22).
    * 미장 보유 0건(API output1 수량 전부 0 + 장부 US 없음)은 정상 상태로 **로그 없이** ``[]`` 반환.
    * 주말·점검 창 또는 **비장중**에는 KIS 실보유 API를 호출하지 않고 장부만 사용.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from api.kis_parsers import parse_us_qty
from utils.helpers import is_coin_ticker


def _skip_kis_equities_live(
    market: str,
    *,
    is_weekend: Callable[[], bool],
    is_market_open: Callable[[str], bool] | None = None,
) -> bool:
    """주말·점검 창이거나 해당 시장이 비장중이면 KIS 실보유 조회를 생략한다."""
    if is_weekend():
        return True
    if is_market_open is None:
        return False
    try:
        return not bool(is_market_open(market))
    except Exception:
        return False


def _ledger_us_codes(pos: dict[str, Any]) -> list[str]:
    return [t for t in pos if (not str(t).isdigit() and not is_coin_ticker(str(t)))]


def get_held_stocks_kr(
    *,
    is_weekend,
    load_state,
    state_path,
    get_balance_with_retry,
    to_float,
    normalize_ticker,
    is_market_open: Callable[[str], bool] | None = None,
):
    if _skip_kis_equities_live("KR", is_weekend=is_weekend, is_market_open=is_market_open):
        try:
            st = load_state(state_path)
            pos = st.get("positions") or {}
            codes = [t for t in pos if str(t).isdigit()]
            print(f"  📌 [조회 facade KR] 비장·점검 억제 — 장부 기반 보유 {len(codes)}종 (API 미호출)")
            return codes
        except Exception as e:
            print(f"  ⚠️ [조회 facade KR] 비장·점검 장부 로드 실패 — 빈 보유: {type(e).__name__}: {e}")
            return []
    try:
        bal = get_balance_with_retry()
        if not bal:
            print("❌ [국장 조회 실패] 잔고 API 응답 없음")
            return None
        if "output1" not in bal:
            print("❌ [국장 조회 실패] output1 필드 없음")
            return None
        held = []
        for s in bal["output1"]:
            hldg_qty = to_float(s.get("hldg_qty", 0))
            ccld_qty = to_float(s.get("ccld_qty_smtl1", 0))
            if hldg_qty > 0.0001 or ccld_qty > 0.0001:
                code = normalize_ticker(s.get("pdno", ""))
                if code:
                    held.append(code)
        return held
    except Exception as e:
        print(f"❌ [국장 조회 실패] {type(e).__name__}: {e}")
        return None


def get_held_stocks_us(
    *,
    is_weekend,
    load_state,
    state_path,
    get_us_positions_with_retry,
    to_float,
    normalize_ticker,
    is_market_open: Callable[[str], bool] | None = None,
):
    if _skip_kis_equities_live("US", is_weekend=is_weekend, is_market_open=is_market_open):
        try:
            st = load_state(state_path)
            pos = st.get("positions") or {}
            codes = _ledger_us_codes(pos)
            print(f"  📌 [조회 facade US] 비장·점검 억제 — 장부 기반 보유 {len(codes)}종 (API 미호출)")
            return codes
        except Exception as e:
            print(f"  ⚠️ [조회 facade US] 비장·점검 장부 로드 실패 — 빈 보유: {type(e).__name__}: {e}")
            return []
    try:
        bal = get_us_positions_with_retry()
        if not bal or "output1" not in bal:
            print("❌ [미장 조회 실패] 잔고 API 응답 없음")
            return None
        held = []
        for s in bal["output1"]:
            qty = parse_us_qty(s, to_float)
            code = normalize_ticker(s.get("ovrs_pdno", s.get("pdno", "")))
            if qty > 0 and code:
                held.append(code)
        return held
    except Exception as e:
        print(f"❌ [미장 조회 실패] {type(e).__name__}: {e}")
        return None


def get_held_stocks_kr_info(
    *,
    is_weekend,
    load_state,
    state_path,
    get_balance_with_retry,
    to_float,
    kr_name_dict,
    ledger_qty_for_ui,
    is_market_open: Callable[[str], bool] | None = None,
):
    if _skip_kis_equities_live("KR", is_weekend=is_weekend, is_market_open=is_market_open):
        try:
            st = load_state(state_path)
            pos = st.get("positions") or {}
            rows = [
                {"code": t, "name": kr_name_dict.get(t, t), "qty": ledger_qty_for_ui(pos.get(t), 1.0)}
                for t in pos
                if str(t).isdigit()
            ]
            print(f"  📌 [조회 facade KR info] 비장·점검 억제 — 장부 기반 {len(rows)}행 (API 미호출)")
            return rows
        except Exception as e:
            print(f"  ⚠️ [조회 facade KR info] 비장·점검 장부 로드 실패: {type(e).__name__}: {e}")
            return []
    try:
        bal = get_balance_with_retry()
        if bal and "output1" in bal:
            return [
                {"code": s["pdno"], "name": kr_name_dict.get(s["pdno"], s.get("prdt_name", "")), "qty": to_float(s.get("hldg_qty"))}
                for s in bal["output1"]
                if to_float(s.get("hldg_qty")) > 0
            ]
        print("  ⚠️ [조회 facade KR info] 잔고 응답 없음 또는 output1 없음 — 빈 리스트 반환")
        return []
    except Exception as e:
        print(f"  ⚠️ [조회 facade KR info] 예외 — 빈 리스트: {type(e).__name__}: {e}")
        return []


def get_held_stocks_us_info(
    *,
    is_weekend,
    load_state,
    state_path,
    get_us_positions_with_retry,
    to_float,
    us_name_dict,
    ledger_qty_for_ui,
    is_market_open: Callable[[str], bool] | None = None,
):
    def _from_ledger():
        try:
            st = load_state(state_path)
            pos = st.get("positions") or {}
            return [
                {"code": t, "name": us_name_dict.get(t, t), "qty": ledger_qty_for_ui(pos.get(t), 1.0)}
                for t in _ledger_us_codes(pos)
            ]
        except Exception as e:
            print(f"  ⚠️ [조회 facade US info] 장부 폴백 실패: {type(e).__name__}: {e}")
            return []

    def _fallback_from_ledger_or_empty(*, reason: str) -> list:
        rows = _from_ledger()
        if rows:
            print(f"  📌 [조회 facade US info] {reason} — 장부 {len(rows)}행 사용")
        return rows

    if _skip_kis_equities_live("US", is_weekend=is_weekend, is_market_open=is_market_open):
        rows = _from_ledger()
        print(f"  📌 [조회 facade US info] 비장·점검 억제 — 장부 기반 {len(rows)}행 (API 미호출)")
        return rows
    try:
        bal = get_us_positions_with_retry()
        if bal and "output1" in bal:
            out = [
                {"code": s["ovrs_pdno"], "name": us_name_dict.get(s["ovrs_pdno"], s.get("ovrs_item_name", "")), "qty": parse_us_qty(s, to_float)}
                for s in bal["output1"]
                if parse_us_qty(s, to_float) > 0
            ]
            if out:
                return out
            return _fallback_from_ledger_or_empty(reason="API 보유 0건")
        return _fallback_from_ledger_or_empty(reason="잔고 응답 없음 또는 output1 없음")
    except Exception as e:
        return _fallback_from_ledger_or_empty(reason=f"예외({type(e).__name__})")


def get_held_stocks_us_detail(
    *,
    is_weekend,
    load_state,
    state_path,
    get_us_positions_with_retry,
    to_float,
    ledger_qty_for_ui,
    is_market_open: Callable[[str], bool] | None = None,
):
    def _from_ledger_detail():
        try:
            st = load_state(state_path)
            pos = st.get("positions") or {}
            out = []
            for code in _ledger_us_codes(pos):
                p = pos.get(code) or {}
                bp = to_float(p.get("buy_p", 0), 0.0)
                out.append({"code": code, "qty": ledger_qty_for_ui(p, 1.0), "avg_p": bp, "current_p": bp})
            return out
        except Exception as e:
            print(f"  ⚠️ [조회 facade US detail] 장부 폴백 실패: {type(e).__name__}: {e}")
            return []

    def _fallback_from_ledger_or_empty(*, reason: str) -> list:
        rows = _from_ledger_detail()
        if rows:
            print(f"  📌 [조회 facade US detail] {reason} — 장부 {len(rows)}행 사용 (현재가=평단)")
        return rows

    if _skip_kis_equities_live("US", is_weekend=is_weekend, is_market_open=is_market_open):
        rows = _from_ledger_detail()
        print(f"  📌 [조회 facade US detail] 비장·점검 억제 — 장부 기반 {len(rows)}행 (현재가=평단, API 미호출)")
        return rows
    try:
        bal = get_us_positions_with_retry()
        if not bal or "output1" not in bal:
            return _fallback_from_ledger_or_empty(reason="잔고 없음 또는 output1 없음")
        result = []
        for item in bal["output1"]:
            qty = parse_us_qty(item, to_float)
            if qty > 0:
                current_p = to_float(item.get("ovrs_now_pric1", item.get("now_pric2", 0)))
                result.append(
                    {
                        "code": item.get("ovrs_pdno", item.get("pdno", "")),
                        "name": (item.get("ovrs_item_name") or item.get("prdt_name") or "").strip(),
                        "qty": qty,
                        "avg_p": to_float(item.get("ovrs_avg_pric", item.get("ovrs_avg_unpr", item.get("avg_unpr3", 0)))),
                        "current_p": current_p,
                    }
                )
        if result:
            return result
        return _fallback_from_ledger_or_empty(reason="API 보유 0건")
    except Exception as e:
        return _fallback_from_ledger_or_empty(reason=f"예외({type(e).__name__})")

