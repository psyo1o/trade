from __future__ import annotations

from api import coin_broker
from utils.helpers import normalize_ticker


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
    get_us_company_name,
    safe_num,
    process_row_data,
):
    rows_data = []

    # 국장: 텔레그램(`get_kr_holdings_with_roi`)과 동일 — 주말·KIS 점검 창에는 API output1을 쓰지 않음
    kr_out = kr_bal.get("output1") if isinstance(kr_bal, dict) else None
    kr_list_ok = isinstance(kr_out, list) and len(kr_out) > 0
    if kr_list_ok and not is_weekend_suppress():
        for item in kr_out:
            qty_num = int(safe_num(item.get("hldg_qty", item.get("ccld_qty_smtl1", 0)), 0))
            if qty_num > 0:
                code = normalize_ticker(str(item.get("pdno", "") or ""))
                price = item.get("pchs_avg_prc", item.get("pchs_avg_pric", "0"))
                current_p = item.get("prpr", "0")
                prdt = (item.get("prdt_name") or item.get("prdt_name1") or "").strip()
                name = prdt or kr_name_dict.get(code) or get_kr_company_name(code) or code
                rows_data.append(process_row_data("🇰🇷 국장", name, str(qty_num), price, "KR", code, current_p))
    # 스냅샷 output1이 비었을 때 — get_held_stocks_kr_info 는 잔고 TTL 캐시를 공유(중복 API 방지)
    elif is_weekend_suppress() or not is_market_open("KR") or not kr_list_ok:
        try:
            st_row = load_state(state_path)
            pos_row = st_row.get("positions") or {}
            for inf in get_held_stocks_kr_info():
                code = str(inf.get("code") or "").strip()
                if not code:
                    continue
                raw_n = (inf.get("name") or "").strip()
                name = raw_n if raw_n and raw_n != code else (get_kr_company_name(code) or code)
                qty_num = max(1, int(safe_num(inf.get("qty"), 1)))
                bp = safe_num(pos_row.get(code, {}).get("buy_p"), 0.0)
                rows_data.append(process_row_data("🇰🇷 국장", name, str(qty_num), bp, "KR", code, None))
        except Exception as e:
            print(f"⚠️ 국장(주말 스냅샷) 테이블 행 구성 실패: {e}")

    # 장중/장외 모두 상세(평단·현재가) 경로 우선 — 텔레그램·현재가 기준 통일
    try:
        us_data = get_held_stocks_us_detail()
        if us_data:
            for item in us_data:
                code, qty, avg_price, current_p = item["code"], item["qty"], item["avg_p"], item.get("current_p", 0.0)
                nm = (item.get("name") or "").strip()
                name = nm or us_name_dict.get(code, code)
                if not nm or name == code:
                    name = get_us_company_name(code) or name
                rows_data.append(process_row_data("🇺🇸 미장", name, str(qty), f"${avg_price:,.2f}", "US", code, current_p))
        else:
            st_row = load_state(state_path)
            pos_row = st_row.get("positions") or {}
            for inf in get_held_stocks_us_info():
                code = str(inf.get("code") or "").strip()
                if not code:
                    continue
                raw_un = (inf.get("name") or "").strip()
                name = raw_un or us_name_dict.get(code, code)
                if not raw_un or name == code:
                    name = get_us_company_name(code) or name
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

    try:
        st_coin = load_state(state_path)
        pos_coin = (st_coin.get("positions") or {}) if isinstance(st_coin, dict) else {}
    except Exception:
        pos_coin = {}

    for coin in upbit_bals:
        cur = str(coin.get("currency") or "").upper()
        if cur in ("KRW", "VTHO"):
            continue
        if coin_config and coin_config.is_binance() and cur == "USDT":
            continue
        if coin_broker.should_include_coin_balance_row(coin):
            if coin_config and coin_config.is_binance():
                code = f"USDT-{coin['currency']}"
            else:
                code = f"KRW-{coin['currency']}"
            qty = str(safe_num(coin.get("balance"), 0.0))
            api_bp = safe_num(coin.get("avg_buy_price", 0), 0.0)
            ledger_bp = safe_num((pos_coin.get(code) or {}).get("buy_p"), 0.0)
            price = str(api_bp if api_bp > 0 else ledger_bp)
            rows_data.append(process_row_data("🪙 코인", coin["currency"], qty, price, "COIN", code, None))

    return rows_data

