import yfinance as yf
import pandas as pd
import pyupbit
import requests
import json
from pathlib import Path

# --- V5.0 백테스트 핵심 엔진 (수익률 계산 탑재) ---
def run_v5_backtest(ticker, data_df, market_name):
    # 기본 투자금 100 (퍼센트 계산용)
    initial_capital = 100.0
    
    if data_df is None or len(data_df) < 200:
        return f"❌ [{ticker}] 데이터 부족 (200일 미만)", initial_capital

    df = data_df.copy()
    
    df['MA50'] = df['Close'].rolling(50).mean()
    df['MA150'] = df['Close'].rolling(150).mean()
    df['MA200'] = df['Close'].rolling(200).mean()
    df['Highest20'] = df['Close'].rolling(20).max()
    
    df['tr0'] = abs(df['High'] - df['Low'])
    df['tr1'] = abs(df['High'] - df['Close'].shift())
    df['tr2'] = abs(df['Low'] - df['Close'].shift())
    df['tr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
    df['ATR'] = df['tr'].rolling(14).mean()

    position = False
    buy_price = 0
    max_price = 0
    hard_stop = 0
    trades = []
    
    current_capital = initial_capital
    
    for i in range(200, len(df)):
        curr = df.iloc[i]
        date = df.index[i].strftime('%Y-%m-%d') if not isinstance(df.index[i], str) else df.index[i]
        c = curr['Close']
        
        if not position:
            if c > curr['MA50'] and curr['MA50'] > curr['MA150'] and curr['MA150'] > curr['MA200']:
                if c >= curr['Highest20'] * 0.97:
                    position = True
                    buy_price = c
                    max_price = c
                    hard_stop = c - (curr['ATR'] * 2.5)
                    trades.append(f"   🔥 [매수] {date} 진입가: {buy_price:,.2f} (방어막: {hard_stop:,.2f})")
        else:
            max_price = max(max_price, c)
            chandelier_exit = max_price - (curr['ATR'] * 3)
            final_exit = max(hard_stop, chandelier_exit)
            
            if c < final_exit:
                profit = (c - buy_price) / buy_price
                current_capital *= (1 + profit) # 복리 적용
                msg = "익절" if profit > 0 else "손절"
                trades.append(f"   🚨 [매도] {date} 샹들리에 붕괴! {msg} 완료 (수익률: {profit*100:+.2f}%)")
                position = False

    if not trades and not position:
        return f"💤 [{ticker}] 최근 1년간 타점 없음 (관망 유지)", current_capital
    else:
        if position:
            current_price = df.iloc[-1]['Close']
            unrealized_profit = (current_price - buy_price) / buy_price
            current_capital *= (1 + unrealized_profit) # 현재 평가금액 반영
            status = f"🛡️ 현재 보유 중 / 📈 진행 수익률: {unrealized_profit*100:+.2f}% (현재가: {current_price:,.2f})"
        else:
            status = f"💸 전량 청산 완료"
            
        trade_log = "\n".join(trades)
        ticker_total_return = (current_capital / initial_capital - 1) * 100
        return f"🎯 [{ticker}] {status} [누적수익: {ticker_total_return:+.2f}%]\n{trade_log}", current_capital

# --- 시장별 데이터 세팅 ---
def test_all_markets():
    print("="*65)
    print("🚀 V5.0 마스터 엔진: 3대 시장 통합 포트폴리오 타임머신 가동")
    print("="*65)

    market_results = {}

    # 1. 🇺🇸 미장 테스트
    us_targets = [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B", "LLY", "AVGO",
        "JPM", "V", "XOM", "UNH", "MA", "PG", "JNJ", "HD", "MRK", "COST",
        "ABBV", "CVX", "CRM", "AMD", "NFLX", "KO", "PEP", "BAC", "TMO", "WMT",
        "ACN", "LIN", "MCD", "CSCO", "ABT", "INTC", "QCOM", "INTU", "VZ", "CMCSA",
        "TXN", "DHR", "PFE", "AMAT", "UNP", "IBM", "NOW", "COP", "PM", "BA"
    ]
    print(f"\n🇺🇸 [미장 테스트 시작] 총 {len(us_targets)}개 대장주 분석 중...")
    us_start_cap = 0
    us_end_cap = 0
    for t in us_targets:
        df = yf.download(t, period="1y", progress=False)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.droplevel(1)
        log, final_cap = run_v5_backtest(t, df, "US")
        print(log)
        us_start_cap += 100
        us_end_cap += final_cap
    market_results['US'] = (us_end_cap / us_start_cap - 1) * 100 if us_start_cap > 0 else 0

    # 2. 🇰🇷 국장 테스트
    kr_start_cap = 0
    kr_end_cap = 0
    kr_file = Path("kr_targets.json")
    if kr_file.exists():
        with open(kr_file, "r", encoding="utf-8") as f:
            kr_targets = json.load(f)
        print(f"\n🇰🇷 [국장 테스트 시작] 스캐너 포착 종목 {len(kr_targets)}개 분석 중...")
        for t in kr_targets:
            df = yf.download(f"{t}.KS", period="1y", progress=False)
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.droplevel(1)
            if df.empty: df = yf.download(f"{t}.KQ", period="1y", progress=False)
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.droplevel(1)
            log, final_cap = run_v5_backtest(t, df, "KR")
            print(log)
            kr_start_cap += 100
            kr_end_cap += final_cap
    else:
        print("\n🇰🇷 [국장 테스트] kr_targets.json 파일이 없어서 패스합니다.")
    market_results['KR'] = (kr_end_cap / kr_start_cap - 1) * 100 if kr_start_cap > 0 else 0

    # 3. 🪙 코인 테스트
    coin_start_cap = 0
    coin_end_cap = 0
    print(f"\n🪙 [코인 테스트 시작] 실시간 거래대금 상위 20개 분석 중...")
    try:
        url = "https://api.upbit.com/v1/ticker?markets=" + ",".join([m['market'] for m in requests.get("https://api.upbit.com/v1/market/all").json() if m['market'].startswith("KRW-")])
        tickers_data = requests.get(url).json()
        coin_targets = [x['market'] for x in sorted(tickers_data, key=lambda x: x['acc_trade_price_24h'], reverse=True)[:20]]
        
        for t in coin_targets:
            df_upbit = pyupbit.get_ohlcv(t, interval="day", count=250)
            if df_upbit is not None:
                df_upbit.rename(columns={'open':'Open', 'high':'High', 'low':'Low', 'close':'Close', 'volume':'Volume'}, inplace=True)
                log, final_cap = run_v5_backtest(t, df_upbit, "COIN")
                print(log)
                coin_start_cap += 100
                coin_end_cap += final_cap
    except Exception as e:
        print(f"⚠️ 코인 데이터 조회 실패: {e}")
    market_results['COIN'] = (coin_end_cap / coin_start_cap - 1) * 100 if coin_start_cap > 0 else 0

    # =======================================================
    # 🏆 최종 계좌 요약 브리핑
    # =======================================================
    print("\n" + "="*65)
    print("🏆 [V5.0 마스터] 3대 시장 포트폴리오 최종 수익률 결산")
    print("="*65)
    print(f"🇺🇸 미장 포트폴리오 총수익률 : {market_results.get('US', 0):+.2f}% (대장주 50개 분산)")
    print(f"🇰🇷 국장 포트폴리오 총수익률 : {market_results.get('KR', 0):+.2f}% (스캐너 종목 분산)")
    print(f"🪙 코인 포트폴리오 총수익률 : {market_results.get('COIN', 0):+.2f}% (거래대금 상위 20개 분산)")
    
    total_start = us_start_cap + kr_start_cap + coin_start_cap
    total_end = us_end_cap + kr_end_cap + coin_end_cap
    grand_total_return = (total_end / total_start - 1) * 100 if total_start > 0 else 0
    
    print("-" * 65)
    print(f"🔥 전체 계좌(국+미+코) 합산 수익률 : {grand_total_return:+.2f}%")
    print("="*65 + "\n")

if __name__ == "__main__":
    test_all_markets()