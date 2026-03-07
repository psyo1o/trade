"""
🇺🇸 미국장 $110 이하 저가 종목 실시간 매수/매도 테스트
- 손실 최소화를 위한 저가 종목
- main64.py 매수 로직 기반
"""

import sys
import json
import time
import traceback
import warnings
warnings.filterwarnings('ignore')

import yfinance as yf
import requests
from datetime import datetime

# =====================================================================
# KIS 설정 (config.json에서 로드)
# =====================================================================
def load_config():
    """config.json에서 KIS 정보 로드"""
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
            return config
    except Exception as e:
        print(f"❌ config.json 로드 실패: {e}")
        return {}

CONFIG = load_config()
KIS_KEY = CONFIG.get('kis_key', '')
KIS_SECRET = CONFIG.get('kis_secret', '')
KIS_ACCOUNT = CONFIG.get('kis_account', '')

# 테스트 예산
TEST_BUY_AMOUNT = 50  # $50 테스트

# =====================================================================
# API 함수
# =====================================================================

def get_us_cash():
    """미국 현물 예수금 조회"""
    try:
        url = "https://openapi.koreainvestment.com:9443/uapi/overseas-stock/v1/trading/inquire-balance"
        headers = {
            "authorization": f"Bearer {KIS_KEY}",
            "content-type": "application/json; charset=utf-8",
            "tr_id": "JTTT3012R",
        }
        params = {
            "CANO": KIS_ACCOUNT[:8],
            "ACNT_PRDT_CD": KIS_ACCOUNT[8:10],
            "OVRS_EXCG_CD": "NASD",
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK": "",
            "CTX_AREA_NK": ""
        }
        
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('rt_cd') == '0':
                output2 = data.get('output2', {})
                return float(output2.get('ovrs_cblc_amt', 0) or 0)
    except Exception as e:
        print(f"❌ 미국 현물 예수금 조회 실패: {e}")
    return 0.0

def get_us_holdings():
    """미국 종목 보유 조회"""
    try:
        url = "https://openapi.koreainvestment.com:9443/uapi/overseas-stock/v1/trading/inquire-balance"
        headers = {
            "authorization": f"Bearer {KIS_KEY}",
            "content-type": "application/json; charset=utf-8",
            "tr_id": "JTTT3012R",
        }
        params = {
            "CANO": KIS_ACCOUNT[:8],
            "ACNT_PRDT_CD": KIS_ACCOUNT[8:10],
            "OVRS_EXCG_CD": "NASD",
            "TR_CRCY_CD": "USD",
            "CTX_AREA_FK": "",
            "CTX_AREA_NK": ""
        }
        
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('rt_cd') == '0':
                return data.get('output1', [])
    except Exception as e:
        print(f"❌ 미국 종목 조회 실패: {e}")
    return []

def buy_us_order(ticker, qty, price):
    """미국장 시장가 매수 (KIS API)"""
    print(f"\n📤 [{ticker}] 매수 주문 발송...")
    print(f"   수량: {qty}")
    print(f"   예정가: ${price:.2f}")
    
    try:
        url = "https://openapi.koreainvestment.com:9443/uapi/overseas-stock/v1/trading/order"
        headers = {
            "authorization": f"Bearer {KIS_KEY}",
            "content-type": "application/json; charset=utf-8",
            "tr_id": "TTTS0307U",
        }
        
        # 101% 지정가 (시장가 구현)
        order_price = round(price * 1.01, 2)
        
        body = {
            "CANO": KIS_ACCOUNT[:8],
            "ACNT_PRDT_CD": KIS_ACCOUNT[8:10],
            "OVRS_EXCG_CD": "NASD",
            "PDNO": ticker,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": str(order_price),
            "SLL_TYPE": "00",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",
            "MGCO_APTM_RSTRD_YN": "N",
            "LOAN_TYPE_CD": "00",
            "TRCS_ADRS_CD": ""
        }
        
        resp = requests.post(url, headers=headers, json=body, timeout=10)
        print(f"   응답 상태: {resp.status_code}")
        
        if resp.status_code == 200:
            data = resp.json()
            print(f"   응답: {data.get('msg1', '')}")
            if data.get('rt_cd') == '0':
                print(f"✅ [미장 매수 체결] {ticker}")
                print(f"   주문가: ${order_price:.2f}")
                print(f"   수량: {qty}")
                return True, order_price
            else:
                print(f"❌ 매수 실패: {data.get('msg1', '')}")
                return False, None
    except Exception as e:
        print(f"❌ 매수 주문 실패: {type(e).__name__}: {e}")
        traceback.print_exc()
    
    return False, None

def sell_us_order(ticker, qty, price):
    """미국장 시장가 매도 (KIS API)"""
    print(f"\n📥 [{ticker}] 매도 주문 발송...")
    print(f"   수량: {qty}")
    print(f"   예정가: ${price:.2f}")
    
    try:
        url = "https://openapi.koreainvestment.com:9443/uapi/overseas-stock/v1/trading/order"
        headers = {
            "authorization": f"Bearer {KIS_KEY}",
            "content-type": "application/json; charset=utf-8",
            "tr_id": "TTTS0308U",
        }
        
        # 99% 지정가 (시장가 구현)
        order_price = round(price * 0.99, 2)
        
        body = {
            "CANO": KIS_ACCOUNT[:8],
            "ACNT_PRDT_CD": KIS_ACCOUNT[8:10],
            "OVRS_EXCG_CD": "NASD",
            "PDNO": ticker,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": str(order_price),
            "SLL_TYPE": "01",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",
            "MGCO_APTM_RSTRD_YN": "N",
            "LOAN_TYPE_CD": "00",
            "TRCS_ADRS_CD": ""
        }
        
        resp = requests.post(url, headers=headers, json=body, timeout=10)
        print(f"   응답 상태: {resp.status_code}")
        
        if resp.status_code == 200:
            data = resp.json()
            print(f"   응답: {data.get('msg1', '')}")
            if data.get('rt_cd') == '0':
                print(f"✅ [미장 매도 체결] {ticker}")
                print(f"   주문가: ${order_price:.2f}")
                print(f"   수량: {qty}")
                return True, order_price
            else:
                print(f"❌ 매도 실패: {data.get('msg1', '')}")
                return False, None
    except Exception as e:
        print(f"❌ 매도 주문 실패: {type(e).__name__}: {e}")
        traceback.print_exc()
    
    return False, None

def get_current_price_us(ticker):
    """yfinance를 통한 현재가 조회"""
    try:
        stock = yf.Ticker(ticker)
        data = stock.history(period="1d")
        if not data.empty:
            return float(data['Close'].iloc[-1])
    except Exception as e:
        print(f"⚠️  {ticker} 현재가 조회 불가: {e}")
    
    return None

def find_low_price_stocks():
    """$110 이하의 인기 종목 검색 (간단한 리스트)"""
    # 실제로는 API로 조회하지만, 테스트용으로 미리 정한 저가 종목
    candidates = [
        "BAC",    # Bank of America ($28-35)
        "T",      # AT&T ($17-25)
        "F",      # Ford ($9-15)
        "NIO",    # NIO ($5-15)
        "GE",     # General Electric ($85-105)
        "AMD",    # AMD ($100-200) - 일부만 해당
        "PLTR",   # Palantir ($20-30)
        "SNAP",   # Snapchat ($15-25)
        "PINS",   # Pinterest ($30-45)
    ]
    
    valid_stocks = []
    print("\n🔍 $110 이하 종목 검색 중...")
    print("-" * 60)
    
    for ticker in candidates:
        price = get_current_price_us(ticker)
        if price and 0 < price <= 110:
            valid_stocks.append((ticker, price))
            print(f"  ✓ {ticker:6s} ${price:8.2f}")
    
    return valid_stocks

# =====================================================================
# 메인 테스트
# =====================================================================
def main():
    print("=" * 60)
    print("🇺🇸 미국장 저가 종목 실시간 매수/매도 테스트")
    print("=" * 60)
    
    if not KIS_KEY or not KIS_SECRET:
        print("❌ KIS 설정이 없습니다!")
        print("   config.json에 kis_key, kis_secret, kis_account를 설정하세요.")
        return
    
    # 1️⃣ 예수금 확인
    print("\n[1단계] 미국장 예수금 확인")
    print("-" * 60)
    us_cash = get_us_cash()
    print(f"  미국 현물 예수금: ${us_cash:.2f}")
    
    if us_cash < TEST_BUY_AMOUNT:
        print(f"❌ 예수금 부족! (필요: ${TEST_BUY_AMOUNT}, 보유: ${us_cash:.2f})")
        return
    
    # 2️⃣ 저가 종목 검색
    print("\n[2단계] $110 이하 저가 종목 검색")
    print("-" * 60)
    candidates = find_low_price_stocks()
    
    if not candidates:
        print("❌ 저가 종목을 찾을 수 없습니다!")
        return
    
    # 3️⃣ 테스트 대상 선택
    print("\n[3단계] 테스트 대상 선택")
    print("-" * 60)
    print(f"  발견된 종목: {len(candidates)}개")
    target_ticker, target_price = candidates[0]
    print(f"  선택 종목: {target_ticker} (${target_price:.2f})")
    
    # 4️⃣ 매수 수량 계산
    print("\n[4단계] 매수 수량 계산")
    print("-" * 60)
    print(f"  테스트 예산: ${TEST_BUY_AMOUNT}")
    print(f"  현재가: ${target_price:.2f}")
    buy_qty = int(TEST_BUY_AMOUNT / target_price)
    print(f"  계획 수량: {buy_qty}주")
    print(f"  예상 지불: ${buy_qty * target_price:.2f}")
    
    if buy_qty <= 0:
        print("❌ 매수 수량이 0입니다!")
        return
    
    # 5️⃣ 매수 확인
    print("\n[5단계] 매수 확인")
    print("-" * 60)
    confirm = input(f"  {target_ticker} {buy_qty}주를 ${target_price:.2f}에 매수하시겠습니까? (yes/no): ").strip().lower()
    
    if confirm != "yes":
        print("❌ 매수 취소")
        return
    
    time.sleep(1)
    
    # 6️⃣ 실제 매수
    print("\n[6단계] 실제 매수 실행")
    print("-" * 60)
    buy_ok, buy_price = buy_us_order(target_ticker, buy_qty, target_price)
    
    if not buy_ok:
        print("❌ 매수 실패!")
        return
    
    time.sleep(3)
    
    # 7️⃣ 가격 모니터링
    print("\n[7단계] 가격 모니터링 (30초)")
    print("-" * 60)
    for i in range(6):
        time.sleep(5)
        current_p = get_current_price_us(target_ticker)
        if current_p:
            profit = (current_p - buy_price) * buy_qty
            profit_rate = ((current_p - buy_price) / buy_price * 100) if buy_price > 0 else 0
            print(f"  {i+1:2d}. 현재가: ${current_p:8.2f} | 손익: ${profit:+8.2f} | 수익률: {profit_rate:+6.2f}%")
    
    # 8️⃣ 매도 여부 확인
    print("\n[8단계] 매도 확인")
    print("-" * 60)
    final_price = get_current_price_us(target_ticker)
    if final_price:
        final_profit = (final_price - buy_price) * buy_qty
        final_profit_rate = ((final_price - buy_price) / buy_price * 100) if buy_price > 0 else 0
        print(f"  매수가: ${buy_price:.2f}")
        print(f"  현재가: ${final_price:.2f}")
        print(f"  손익: ${final_profit:+.2f}")
        print(f"  수익률: {final_profit_rate:+.2f}%")
    
    confirm = input(f"  {target_ticker}를 매도하시겠습니까? (yes/no): ").strip().lower()
    
    if confirm != "yes":
        print("⏳ 매도 연기")
        return
    
    # 9️⃣ 실제 매도
    print("\n[9단계] 실제 매도 실행")
    print("-" * 60)
    sell_ok, sell_price = sell_us_order(target_ticker, buy_qty, final_price)
    
    if not sell_ok:
        print("❌ 매도 실패!")
        return
    
    time.sleep(2)
    
    # 🔟 최종 정산
    print("\n[10단계] 최종 정산")
    print("-" * 60)
    if sell_price:
        total_profit = (sell_price - buy_price) * buy_qty
        total_profit_rate = ((sell_price - buy_price) / buy_price * 100) if buy_price > 0 else 0
        commission = TEST_BUY_AMOUNT * 0.001  # 대략 0.1% 수수료 고려
        
        print(f"  매수가: ${buy_price:.2f} × {buy_qty}주")
        print(f"  매도가: ${sell_price:.2f} × {buy_qty}주")
        print(f"  총 손익: ${total_profit:+.2f}")
        print(f"  수익률: {total_profit_rate:+.2f}%")
        
        if total_profit > 0:
            print(f"\n✅ [테스트 성공] 수익 ${total_profit:+.2f} 달성!")
        elif total_profit == 0:
            print(f"\n⚪ [테스트 완료] 손익 분기점")
        else:
            print(f"\n⚠️  [테스트 손실] 손실 ${total_profit:+.2f}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  사용자 중단")
        sys.exit(0)
    except Exception as e:
        print(f"\n\n❌ 예상치 못한 오류: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
