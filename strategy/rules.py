import pandas as pd
import numpy as np
import time

import yfinance as yf

_NAME_CACHE = {}

def _is_valid_symbol_name(name, ticker):
    text = str(name or "").strip()
    base = str(ticker or "").strip()
    if not text or text == base:
        return False
    if "," in text:
        return False
    upper = text.upper()
    if upper.startswith(f"{base}.KS") or upper.startswith(f"{base}.KQ"):
        return False
    return True

def _resolve_display_name(ticker, name=""):
    if _is_valid_symbol_name(name, ticker):
        return str(name)

    key = str(ticker)
    if key.startswith("KRW-"):
        _NAME_CACHE[key] = key
        return key

    if key in _NAME_CACHE:
        return _NAME_CACHE[key]

    resolved = key
    try:
        if key.isdigit():
            info = yf.Ticker(f"{key}.KS").info
            name_yf = info.get('longName', info.get('shortName'))
            if _is_valid_symbol_name(name_yf, key):
                resolved = str(name_yf)
            else:
                info = yf.Ticker(f"{key}.KQ").info
                name_yf = info.get('longName', info.get('shortName'))
                if _is_valid_symbol_name(name_yf, key):
                    resolved = str(name_yf)
        else:
            info = yf.Ticker(key).info
            name_yf = info.get('longName', info.get('shortName'))
            if _is_valid_symbol_name(name_yf, key):
                resolved = str(name_yf)
    except Exception:
        pass

    _NAME_CACHE[key] = resolved
    return resolved

# 🧰 1. 200일선 전략용 (야후 파이낸스 - 과거 데이터 길게 뽑기)
def get_ohlcv_yfinance(ticker):
    try:
        # 🇺🇸 미장 티커: 영문+하이픈만 → .KS 안 붙임
        # 🇰🇷 국내 티커: 숫자만 → .KS 붙임
        if ticker.isdigit():  # 국내주식 (000660 같은 숫자)
            code_yf = ticker + ".KS" if not ticker.endswith(".KS") and not ticker.endswith(".KQ") else ticker
        else:  # 미국주식 (AAPL, BRK-B 등)
            code_yf = ticker
        
        df = yf.download(code_yf, period="1y", interval="1d", progress=False)
        if df.empty and ticker.isdigit():  # 국내주식만 .KQ로 재시도
            code_yf = ticker.replace(".KS", ".KQ")
            df = yf.download(code_yf, period="1y", interval="1d", progress=False)
        
        rows = []
        for index, row in df.iterrows():
            rows.append({
                'o': float(row['Open']), 'h': float(row['High']),
                'l': float(row['Low']), 'c': float(row['Close']), 'v': float(row['Volume'])
            })
        return rows
    except Exception as e:
        print(f"⚠️ 야후 데이터 에러: {e}")
        return []

# 🧰 2. 5% 갭상승 컷오프용 (한투 API - 아침 9시 딜레이 없는 실시간 타격)
def get_ohlcv_realtime(broker, ticker):
    """국내는 KIS 직통, 해외는 yfinance로 OHLCV 조회"""
    try:
        import requests

        if not str(ticker).isdigit():
            return get_ohlcv_yfinance(ticker)

        clean_token = str(getattr(broker, "access_token", "") or "").replace("Bearer ", "").strip()
        base_url = getattr(broker, "base_url", "https://openapi.koreainvestment.com:9443")
        is_mock = "vps" in base_url or "vts" in base_url

        tr_id = "FHKST03010100"
        url = f"{base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {clean_token}",
            "appkey": getattr(broker, "api_key", ""),
            "appsecret": getattr(broker, "api_secret", ""),
            "tr_id": tr_id,
            "custtype": "P",
        }
        from datetime import datetime, timedelta
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=380)).strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": str(ticker),
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "1",
        }

        data = {}
        max_retries = 3
        for attempt in range(max_retries):
            try:
                res = requests.get(url, headers=headers, params=params, timeout=10)
                data = res.json() if res is not None else {}
            except Exception as req_err:
                print(f"     🔴 OHLCV 네트워크 오류 [{attempt+1}/{max_retries}] ({ticker}): {type(req_err).__name__}: {req_err}")
                data = {"rt_cd": "EX", "msg1": f"{type(req_err).__name__}: {req_err}"}

            if str(data.get("rt_cd", "")) == "0":
                break

            msg = str(data.get("msg1", "") or "")
            if attempt < max_retries - 1:
                print(f"     ⚠️  OHLCV 재시도 [{attempt+1}/{max_retries}] ({ticker}): {msg[:50]}")
                time.sleep(0.3 * (attempt + 1))

        if str(data.get("rt_cd", "")) != "0":
            print(f"     🔴 OHLCV 조회 실패 ({ticker}): rt_cd={data.get('rt_cd')}, msg={data.get('msg1', 'N/A')}")
            return []

        output2 = data.get("output2", [])
        if not output2:
            print(f"     ⚠️ KIS OHLCV 비어있음 ({ticker}) → yfinance 대체 조회")
            return get_ohlcv_yfinance(ticker)

        rows = []
        for item in reversed(output2):
            try:
                rows.append({
                    "o": float(item.get("open", item.get("stck_oprc", 0))),
                    "h": float(item.get("high", item.get("stck_hgpr", 0))),
                    "l": float(item.get("low", item.get("stck_lwpr", 0))),
                    "c": float(item.get("close", item.get("stck_clpr", 0))),
                    "v": float(item.get("volume", item.get("acml_vol", 0))),
                })
            except (ValueError, TypeError):
                continue
        return rows
    except Exception as e:
        print(f"     🔴 get_ohlcv_realtime({ticker}) → 예외: {type(e).__name__}: {e}")
        return []


# 🧠 V5.0 기관급 매수 엔진: "오직 강한 놈만 더 비싸게 산다"
# 🧠 [수다쟁이 V5.0 엔진] 패스하는 이유를 낱낱이 불어라!
def calculate_pro_signals(ohlcv, market_weather, ticker="", name="", idx=0, total=0):
    # 구버전 호출 호환: calculate_pro_signals(ohlcv, weather, ticker, idx, total)
    if isinstance(name, (int, float)) and isinstance(idx, (int, float)) and (not total or total == 0):
        idx, total = int(name), int(idx)
        name = ""

    name = _resolve_display_name(ticker, name)
    progress = f"[{idx}/{total}]" if total > 0 else ""
    display_name = f"{name}({ticker})" if name and name != ticker else ticker
    
    if not ohlcv or len(ohlcv) < 200: 
        print(f"   🔍 {progress} {display_name} ❌ 패스: 데이터 부족 (200일 미만)")
        return False, 0, ""
    
    import pandas as pd
    df = pd.DataFrame(ohlcv)
    curr = df.iloc[-1]
    
    ma50 = df['c'].rolling(50).mean().iloc[-1]
    ma150 = df['c'].rolling(150).mean().iloc[-1]
    ma200 = df['c'].rolling(200).mean().iloc[-1]
    highest_20 = df['c'].rolling(20).max().iloc[-1]
    
    df['tr0'] = abs(df['h'] - df['l'])
    df['tr1'] = abs(df['h'] - df['c'].shift())
    df['tr2'] = abs(df['l'] - df['c'].shift())
    df['tr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
    atr = df['tr'].rolling(14).mean().iloc[-1]
    
    if market_weather == "🌧️ BEAR":
        print(f"   🔍 {progress} {display_name} ❌ 패스: 시장이 하락장(BULL 아님)")
        return False, 0, ""
        
    if not (curr['c'] > ma50 and ma50 > ma150 and ma150 > ma200):
        print(f"   🔍 {progress} {display_name} ❌ 패스: 50/150/200 정배열 실패 (현재가:{curr['c']:.0f})")
        return False, 0, ""
        
    if curr['c'] < highest_20 * 0.95:
        print(f"   🔍 {progress} {display_name} ❌ 패스: 모멘텀 부족 (고점대비 95% 미달)")
        return False, 0, ""

    stop_loss = curr['c'] - (atr * 2.5)
    print(f"   🔥 {progress} {display_name} 🎯 V5.0 타점 완벽 일치! (매수 대기)")
    return True, stop_loss, "V5.0 기관 추세돌파"

# 🛑 V5.0 기관급 매도 엔진: "샹들리에 트레일링 스탑 (Chandelier Exit)"
def check_pro_exit(curr_p, pos_info, ohlcv):
    if not ohlcv: return False, ""
    df = pd.DataFrame(ohlcv)
    
    # ATR 다시 계산 (행별 전일종가 정상 계산)
    df['prev_c'] = df['c'].shift(1)
    df['tr'] = df.apply(lambda x: max(x['h'] - x['l'], abs(x['h'] - x['prev_c']) if pd.notna(x['prev_c']) else 0, abs(x['l'] - x['prev_c']) if pd.notna(x['prev_c']) else 0), axis=1)
    atr = df['tr'].rolling(14).mean().iloc[-1]
    
    # 매수 이후 내가 경험한 가장 높은 가격 (최고점)
    max_price = max(pos_info.get('max_p', curr_p), curr_p)
    
    # 💎 익절/손절선: 고점에서 ATR의 3배만큼 뺀 가격 (이 선이 계속 따라올라감)
    chandelier_exit = max_price - (atr * 3)
    
    # 최초 매수 시 설정한 하드 스탑(방어막)과 샹들리에 익절선 중 더 '높은' 가격을 현재 나의 기준선으로 잡음
    hard_stop = pos_info.get('sl_p', curr_p * 0.9)
    final_exit_line = max(hard_stop, chandelier_exit)
    
    if curr_p < final_exit_line:
        return True, "V5.0 샹들리에 라인 붕괴 (추세 종료)"
        
    return False, ""