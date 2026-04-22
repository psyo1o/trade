"""
account_read_facade — 국·미 보유 조회의 **단일 진입점**.

역할
    * KIS ``output1`` 기반 실보유 목록·수량·(가능 시) 현재가를 조합한다.
    * **주말/점검** 구간에서는 API를 부르지 않고 ``bot_state.json`` 의 ``positions`` 로 폴백한다.

로그 정책
    * API 실패·필드 누락은 ``❌`` 로 즉시 출력(기존 유지).
    * 장부 폴백·빈 응답도 **조용히 빈 리스트를 반환하지 않고** 이유를 한 줄 남긴다(2026-04-22).
"""
from __future__ import annotations

from api.kis_parsers import parse_us_qty


def get_held_stocks_kr(
    *,
    is_weekend,
    load_state,
    state_path,
    get_balance_with_retry,
    to_float,
    normalize_ticker,
):
    if is_weekend():
        try:
            st = load_state(state_path)
            pos = st.get("positions") or {}
            codes = [t for t in pos if str(t).isdigit()]
            print(f"  📌 [조회 facade KR] 주말·점검 창 — 장부 기반 보유 {len(codes)}종 (API 미호출)")
            return codes
        except Exception as e:
            print(f"  ⚠️ [조회 facade KR] 주말 장부 로드 실패 — 빈 보유: {type(e).__name__}: {e}")
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
):
    if is_weekend():
        try:
            st = load_state(state_path)
            pos = st.get("positions") or {}
            codes = [t for t in pos if (not str(t).isdigit() and not str(t).upper().startswith("KRW-"))]
            print(f"  📌 [조회 facade US] 주말·점검 창 — 장부 기반 보유 {len(codes)}종 (API 미호출)")
            return codes
        except Exception as e:
            print(f"  ⚠️ [조회 facade US] 주말 장부 로드 실패 — 빈 보유: {type(e).__name__}: {e}")
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
):
    if is_weekend():
        try:
            st = load_state(state_path)
            pos = st.get("positions") or {}
            rows = [
                {"code": t, "name": kr_name_dict.get(t, t), "qty": ledger_qty_for_ui(pos.get(t), 1.0)}
                for t in pos
                if str(t).isdigit()
            ]
            print(f"  📌 [조회 facade KR info] 주말·점검 — 장부 기반 {len(rows)}행 (API 미호출)")
            return rows
        except Exception as e:
            print(f"  ⚠️ [조회 facade KR info] 주말 장부 로드 실패: {type(e).__name__}: {e}")
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
):
    def _from_ledger():
        try:
            st = load_state(state_path)
            pos = st.get("positions") or {}
            return [
                {"code": t, "name": us_name_dict.get(t, t), "qty": ledger_qty_for_ui(pos.get(t), 1.0)}
                for t in pos
                if not str(t).isdigit() and not str(t).upper().startswith("KRW-")
            ]
        except Exception as e:
            print(f"  ⚠️ [조회 facade US info] 장부 폴백 실패: {type(e).__name__}: {e}")
            return []

    if is_weekend():
        rows = _from_ledger()
        print(f"  📌 [조회 facade US info] 주말·점검 — 장부 기반 {len(rows)}행 (API 미호출)")
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
            print("  ⚠️ [조회 facade US info] output1에 유효 수량 없음 — 장부 폴백")
            return _from_ledger()
        print("  ⚠️ [조회 facade US info] 잔고 응답 없음 또는 output1 없음 — 장부 폴백")
        return _from_ledger()
    except Exception as e:
        print(f"  ⚠️ [조회 facade US info] 예외 — 장부 폴백: {type(e).__name__}: {e}")
        return _from_ledger()


def get_held_stocks_us_detail(
    *,
    is_weekend,
    load_state,
    state_path,
    get_us_positions_with_retry,
    to_float,
    ledger_qty_for_ui,
):
    def _from_ledger_detail():
        try:
            st = load_state(state_path)
            pos = st.get("positions") or {}
            out = []
            for code, p in pos.items():
                if str(code).isdigit() or str(code).upper().startswith("KRW-"):
                    continue
                bp = to_float(p.get("buy_p", 0), 0.0)
                out.append({"code": code, "qty": ledger_qty_for_ui(p, 1.0), "avg_p": bp, "current_p": bp})
            return out
        except Exception as e:
            print(f"  ⚠️ [조회 facade US detail] 장부 폴백 실패: {type(e).__name__}: {e}")
            return []

    if is_weekend():
        rows = _from_ledger_detail()
        print(f"  📌 [조회 facade US detail] 주말·점검 — 장부 기반 {len(rows)}행 (현재가=평단, API 미호출)")
        return rows
    try:
        bal = get_us_positions_with_retry()
        if not bal or "output1" not in bal:
            print("  ⚠️ [조회 facade US detail] 잔고 없음 또는 output1 없음 — 장부 폴백(현재가=평단)")
            return _from_ledger_detail()
        result = []
        for item in bal["output1"]:
            qty = parse_us_qty(item, to_float)
            if qty > 0:
                current_p = to_float(item.get("ovrs_now_pric1", item.get("now_pric2", 0)))
                result.append(
                    {
                        "code": item.get("ovrs_pdno", item.get("pdno", "")),
                        "qty": qty,
                        "avg_p": to_float(item.get("ovrs_avg_pric", item.get("ovrs_avg_unpr", item.get("avg_unpr3", 0)))),
                        "current_p": current_p,
                    }
                )
        if result:
            return result
        print("  ⚠️ [조회 facade US detail] output1에 유효 수량 없음 — 장부 폴백")
        return _from_ledger_detail()
    except Exception as e:
        print(f"  ⚠️ [조회 facade US detail] 예외 — 장부 폴백: {type(e).__name__}: {e}")
        return _from_ledger_detail()

