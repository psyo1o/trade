from __future__ import annotations


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

