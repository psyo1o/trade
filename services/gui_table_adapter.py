from __future__ import annotations


def build_rows_data(
    *,
    kr_bal,
    upbit_bals,
    is_market_open,
    is_weekend_suppress,
    load_state,
    state_path,
    get_held_stocks_kr_info,
    get_held_stocks_us_info,
    get_held_stocks_us_detail,
    kr_name_dict,
    us_name_dict,
    get_kr_company_name,
    safe_num,
    process_row_data,
):
    rows_data = []

    # 국장 파싱
    if isinstance(kr_bal, dict) and "output1" in kr_bal and isinstance(kr_bal["output1"], list):
        for item in kr_bal["output1"]:
            qty_num = int(safe_num(item.get("hldg_qty", item.get("ccld_qty_smtl1", 0)), 0))
            if qty_num > 0:
                code = item.get("pdno", "")
                price = item.get("pchs_avg_prc", item.get("pchs_avg_pric", "0"))
                current_p = item.get("prpr", "0")
                name = kr_name_dict.get(code) or get_kr_company_name(code) or code
                rows_data.append(process_row_data("🇰🇷 국장", name, str(qty_num), price, "KR", code, current_p))
    elif is_weekend_suppress() or not is_market_open("KR"):
        try:
            st_row = load_state(state_path)
            pos_row = st_row.get("positions") or {}
            for inf in get_held_stocks_kr_info():
                code = str(inf.get("code") or "").strip()
                if not code:
                    continue
                name = inf.get("name") or code
                qty_num = max(1, int(safe_num(inf.get("qty"), 1)))
                bp = safe_num(pos_row.get(code, {}).get("buy_p"), 0.0)
                rows_data.append(process_row_data("🇰🇷 국장", name, str(qty_num), bp, "KR", code, None))
        except Exception as e:
            print(f"⚠️ 국장(주말 스냅샷) 테이블 행 구성 실패: {e}")

    # 미장 파싱: 장중/장외 모두 상세 경로 우선(텔레그램과 현재가 기준 통일)
    try:
        us_data = get_held_stocks_us_detail()
        if us_data:
            for item in us_data:
                code, qty, avg_price, current_p = item["code"], item["qty"], item["avg_p"], item.get("current_p", 0.0)
                name = us_name_dict.get(code, code)
                rows_data.append(process_row_data("🇺🇸 미장", name, str(qty), f"${avg_price:,.2f}", "US", code, current_p))
        else:
            st_row = load_state(state_path)
            pos_row = st_row.get("positions") or {}
            for inf in get_held_stocks_us_info():
                code = str(inf.get("code") or "").strip()
                if not code:
                    continue
                name = inf.get("name") or us_name_dict.get(code, code)
                qty_num = max(1, int(safe_num(inf.get("qty"), 1)))
                bp = safe_num(pos_row.get(code, {}).get("buy_p"), 0.0)
                rows_data.append(process_row_data("🇺🇸 미장", name, str(qty_num), f"${bp:,.2f}", "US", code, None))
    except Exception as e:
        print(f"⚠️ 미장 테이블 행 구성 실패: {e}")

    # 코인 파싱
    try:
        from api import coin_broker, coin_config
    except Exception:
        coin_broker = None
        coin_config = None

    for coin in upbit_bals:
        cur = str(coin.get("currency") or "").upper()
        if cur in ("KRW", "VTHO"):
            continue
        if coin_config and coin_config.is_binance() and cur == "USDT":
            continue
        if safe_num(coin.get("balance"), 0.0) > 0.00000001:
            if coin_config and coin_config.is_binance():
                code = f"USDT-{coin['currency']}"
            else:
                code = f"KRW-{coin['currency']}"
            qty = str(safe_num(coin.get("balance"), 0.0))
            price = str(safe_num(coin.get("avg_buy_price", 0), 0.0))
            rows_data.append(process_row_data("🪙 코인", coin["currency"], qty, price, "COIN", code, None))

    return rows_data

