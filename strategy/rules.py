import pandas as pd
import numpy as np

import yfinance as yf

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
    try:
        resp = broker.fetch_ohlcv(ticker, timeframe='D', adj_price=True)
        if not resp or 'output2' not in resp: return []
        
        rows = []
        for item in reversed(resp['output2']):
            rows.append({
                'o': float(item['stck_oprc']), 'h': float(item['stck_hgpr']),
                'l': float(item['stck_lwpr']), 'c': float(item['stck_clpr']), 'v': float(item['acml_vol'])
            })
        return rows
    except Exception as e:
        print(f"⚠️ 실시간 API 에러: {e}")
        return []


# 🧠 V5.0 기관급 매수 엔진: "오직 강한 놈만 더 비싸게 산다"
# 🧠 [수다쟁이 V5.0 엔진] 패스하는 이유를 낱낱이 불어라!
def calculate_pro_signals(ohlcv, market_weather, ticker="", idx=0, total=0):
    progress = f"[{idx}/{total}]" if total > 0 else ""
    
    if not ohlcv or len(ohlcv) < 200: 
        print(f"   🔍 {progress} {ticker} ❌ 패스: 데이터 부족 (200일 미만)")
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
        print(f"   🔍 {progress} {ticker} ❌ 패스: 시장이 하락장(BULL 아님)")
        return False, 0, ""
        
    if not (curr['c'] > ma50 and ma50 > ma150 and ma150 > ma200):
        print(f"   🔍 {progress} {ticker} ❌ 패스: 50/150/200 정배열 실패 (현재가:{curr['c']:.0f})")
        return False, 0, ""
        
    if curr['c'] < highest_20 * 0.95:
        print(f"   🔍 {progress} {ticker} ❌ 패스: 모멘텀 부족 (고점대비 95% 미달)")
        return False, 0, ""

    stop_loss = curr['c'] - (atr * 2.5)
    print(f"   🔥 {progress} {ticker} 🎯 V5.0 타점 완벽 일치! (매수 대기)")
    return True, stop_loss, "V5.0 기관 추세돌파"

# 🛑 V5.0 기관급 매도 엔진: "샹들리에 트레일링 스탑 (Chandelier Exit)"
def check_pro_exit(curr_p, pos_info, ohlcv):
    if not ohlcv: return False, ""
    df = pd.DataFrame(ohlcv)
    
    # ATR 다시 계산
    df['tr'] = df[['h', 'l', 'c']].apply(lambda x: max(x['h'] - x['l'], abs(x['h'] - df['c'].shift(1).iloc[-1]), abs(x['l'] - df['c'].shift(1).iloc[-1])), axis=1)
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