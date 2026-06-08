from __future__ import annotations


def kis_response_rate_limited(bal) -> bool:
    """KIS 초당 거래건수 초과(EGW00201 등) 여부."""
    if not isinstance(bal, dict) or not bal:
        return False
    msg = str(bal.get("msg1") or bal.get("MSG1") or "")
    cd = str(bal.get("msg_cd") or bal.get("MSG_CD") or "")
    blob = f"{cd} {msg}"
    if "EGW00201" in blob:
        return True
    if "초당" in msg and "거래" in msg:
        return True
    return False


def kis_response_transient(bal) -> bool:
    """짧은 백오프 재시도 대상(한도·MCI·OPSQ 등)."""
    if kis_response_rate_limited(bal):
        return True
    if not isinstance(bal, dict):
        return False
    msg = str(bal.get("msg1") or bal.get("MSG1") or "")
    if "OPSQ0008" in msg or "MCI" in msg:
        return True
    return False


def kis_response_ok(
    bal,
    *,
    require_output2: bool = False,
    require_output1: bool = False,
) -> tuple[bool, str]:
    """
    KIS 잔고·보유 응답이 **정상(rt_cd=0)** 인지.

    ``require_output2`` / ``require_output1`` — Phase5 합산·예수금 파싱용 필드 존재 여부.
    """
    if not isinstance(bal, dict) or not bal:
        return False, "empty_response"
    rt = str(bal.get("rt_cd", bal.get("RT_CD", "0")) or "0").strip()
    if rt and rt != "0":
        msg = str(bal.get("msg1") or bal.get("MSG1") or "").strip()
        cd = str(bal.get("msg_cd") or bal.get("MSG_CD") or "").strip()
        detail = " ".join(x for x in (cd, msg) if x)
        return False, f"rt_cd={rt}" + (f" ({detail})" if detail else "")
    if require_output2 and "output2" not in bal:
        return False, "no_output2"
    if require_output1:
        o1 = bal.get("output1")
        if not isinstance(o1, list) or len(o1) == 0:
            return False, "no_output1"
    return True, "ok"


def as_row_dict(output2):
    """KIS output2(list|dict)를 단일 dict row로 정규화."""
    if isinstance(output2, list):
        return output2[0] if output2 else {}
    if isinstance(output2, dict):
        return output2
    return {}


def parse_kr_cash_total(output2, to_float):
    """KR output2에서 예수금/총평가를 추출."""
    row = as_row_dict(output2)
    cash = int(to_float(row.get("prvs_rcdl_excc_amt", 0)))
    total = int(to_float(row.get("tot_evlu_amt", cash)))
    return cash, total


def parse_us_cash_fallback(output2, to_float):
    """US output2에서 외화 예수금 fallback 값을 추출."""
    row = as_row_dict(output2)
    return float(to_float(row.get("frcr_dncl_amt_2", row.get("frcr_buy_amt_smtl", 0.0)), 0.0))


def parse_us_qty(item, to_float):
    """US 보유수량 필드 후보를 일관 추출."""
    return float(to_float(item.get("ovrs_cblc_qty", item.get("ccld_qty_smtl1", item.get("hldg_qty", 0)))))


def ensure_output1(balance_data) -> list:
    """KIS 잔고 dict에서 output1 리스트만 추출(없·비정상이면 [])."""
    if not balance_data or "output1" not in balance_data:
        return []
    o1 = balance_data.get("output1")
    return o1 if isinstance(o1, list) else []


_EMPTY_HOLDINGS_METRICS = {
    "invested": 0.0,
    "current": 0.0,
    "profit": 0.0,
    "roi": 0.0,
}


def parse_kr_qty(item: dict, to_float) -> float:
    """KR 보유수량 필드 후보."""
    return float(to_float(item.get("hldg_qty", item.get("ccld_qty_smtl1", 0))))


def parse_kr_live_price(item: dict, to_float) -> float:
    """KR 잔고 output1 행의 현재가 — ``fetch_price`` 대체."""
    px = float(to_float(item.get("prpr", item.get("stck_prpr", 0))))
    return px if px > 0 else 0.0


def parse_us_live_price(item: dict, to_float) -> float:
    """US 잔고 output1 행의 현재가 — ``fetch_price`` 대체."""
    for key in ("ovrs_nmix_prpr", "ovrs_now_pric1", "ovrs_now_prc2", "prpr"):
        px = float(to_float(item.get(key, 0)))
        if px > 0:
            return px
    return 0.0


def parse_kr_holdings_metrics(balance_data, to_float) -> dict:
    """국내 output1 기준 투자·평가·손익·ROI (``_calc_kr_holdings_metrics`` 동일)."""
    rows = ensure_output1(balance_data)
    if not rows:
        return dict(_EMPTY_HOLDINGS_METRICS)
    try:
        total_invested = 0.0
        total_current = 0.0
        for stock in rows:
            qty = parse_kr_qty(stock, to_float)
            if qty > 0:
                avg_price = float(to_float(stock.get("pchs_avg_prc", stock.get("pchs_avg_pric", 0))))
                invested = avg_price * qty
                current_price = float(to_float(stock.get("prpr", stock.get("stck_prpr", 0))))
                current = current_price * qty
                total_invested += invested
                total_current += current
        profit = total_current - total_invested
        roi = (profit / total_invested * 100) if total_invested > 0 else 0.0
        return {
            "invested": total_invested,
            "current": total_current,
            "profit": profit,
            "roi": roi,
        }
    except Exception:
        return dict(_EMPTY_HOLDINGS_METRICS)


def parse_us_qty_for_metrics(item: dict, to_float) -> float:
    """US 보유수량 — ``_calc_us_holdings_metrics`` 와 동일한 필드 우선순위."""
    qty = float(to_float(item.get("ovrs_cblc_qty", item.get("hldg_qty", 0))))
    if qty <= 0:
        qty = float(to_float(item.get("ccld_qty_smtl1", 0)))
    return qty


def parse_us_holdings_metrics(balance_data, to_float) -> dict:
    """미국 output1 기준 투자·평가·손익·ROI (``_calc_us_holdings_metrics`` 동일)."""
    rows = ensure_output1(balance_data)
    if not rows:
        return dict(_EMPTY_HOLDINGS_METRICS)
    try:
        total_invested = 0.0
        total_current = 0.0
        for stock in rows:
            qty = parse_us_qty_for_metrics(stock, to_float)
            if qty > 0:
                avg_price = float(
                    to_float(
                        stock.get(
                            "ovrs_avg_unpr",
                            stock.get("ovrs_avg_pric", stock.get("avg_unpr3", 0)),
                        )
                    )
                )
                invested = avg_price * qty
                current_price = float(
                    to_float(
                        stock.get(
                            "ovrs_now_prc2",
                            stock.get("ovrs_nmix_prpr", stock.get("ovrs_now_pric1", 0)),
                        )
                    )
                )
                current = current_price * qty
                total_invested += invested
                total_current += current
        profit = total_current - total_invested
        roi = (profit / total_invested * 100) if total_invested > 0 else 0.0
        return {
            "invested": total_invested,
            "current": total_current,
            "profit": profit,
            "roi": roi,
        }
    except Exception:
        return dict(_EMPTY_HOLDINGS_METRICS)


def extract_held_kr_codes(kr_output1: list, to_float, normalize_ticker) -> list[str]:
    """KR output1에서 수량>0 보유 종목 코드."""
    held: list[str] = []
    if not isinstance(kr_output1, list):
        return held
    for s in kr_output1:
        if not isinstance(s, dict):
            continue
        qty = float(to_float(s.get("hldg_qty", s.get("t01", s.get("q", 0)))))
        if qty > 0.0001:
            code = normalize_ticker(s.get("pdno", ""))
            if code:
                held.append(code)
    return held


def extract_held_us_codes(us_output1: list, to_float, normalize_ticker) -> list[str]:
    """US output1에서 수량>0 보유 종목 코드."""
    held: list[str] = []
    if not isinstance(us_output1, list):
        return held
    for s in us_output1:
        if not isinstance(s, dict):
            continue
        qty = float(
            to_float(s.get("ovrs_cblc_qty", s.get("ccld_qty_smtl1", s.get("hldg_qty", 0))))
        )
        if qty > 0.0001:
            code = normalize_ticker(s.get("ovrs_pdno", s.get("pdno", "")))
            if code:
                held.append(code)
    return held


def sum_us_output1_stock_value_usd(us_output1: list, to_float) -> float:
    """US output1 행별 평가금 합산(``frcr_evlu_amt2`` 없으면 현재가×수량)."""
    if not isinstance(us_output1, list):
        return 0.0
    total = 0.0
    for s in us_output1:
        if not isinstance(s, dict):
            continue
        val = float(to_float(s.get("frcr_evlu_amt2", 0)))
        if val <= 0:
            price = float(to_float(s.get("ovrs_now_prc2", 0)))
            qty = float(to_float(s.get("ovrs_cblc_qty", s.get("hldg_qty", 0))))
            val = price * qty
        total += val
    return total


def compute_us_stock_value_from_output(us_bal: dict, out2, to_float) -> float:
    """
    US 주식 평가금 — output2 ``ovrs_stck_evlu_amt`` 우선, 0이면 output1 합산 보정.
    보정 시 stdout 로그 1줄(기존 ``_compute_us_stock_value_from_output`` 동일).
    """
    us_output1 = us_bal.get("output1", []) if isinstance(us_bal.get("output1"), list) else []

    if isinstance(out2, list) and out2:
        us_stock_value = float(to_float(out2[0].get("ovrs_stck_evlu_amt", 0)))
    elif isinstance(out2, dict):
        us_stock_value = float(to_float(out2.get("ovrs_stck_evlu_amt", 0)))
    else:
        us_stock_value = 0.0

    if us_stock_value <= 0 and us_output1:
        manual_stock_eval = sum_us_output1_stock_value_usd(us_output1, to_float)
        if manual_stock_eval > 0:
            print(
                f"  🔍 [잔고 보정] output2에 평가금 누락 감지 -> 보유종목 직접 합산: ${manual_stock_eval:.2f}"
            )
            us_stock_value = manual_stock_eval

    return float(us_stock_value)

