"""
🇰🇷 국내장 저가 우량주 실시간 매수/매도 테스트
- main64.py 매수 로직 기반
- 거래 잘 되는 저가 종목 테스트
"""

import sys
import json
import time
import traceback
import warnings
warnings.filterwarnings('ignore')

import yfinance as yf
import requests
import pandas as pd
from datetime import datetime

# =====================================================================
# KIS 설정
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
TEST_BUY_AMOUNT = 100000  # 10만원 테스트

# 테스트 대상 (거래 좋은 저가 우량주)
CANDIDATE_STOCKS = [
    "003550",  # LG화학
    "000370",  # 한화Q CELLS
    "002790",  # 매직마이크로
    "000155",  # 아남
    "000090",  # SK텔레콤
]

# =====================================================================
# API 함수
# =====================================================================

def get_kr_cash():
    """국내 현물 예수금 조회"""
    try:
        url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/inquire-balance"
        headers = {
            "authorization": f"Bearer {KIS_KEY}",
            "content-type": "application/json; charset=utf-8",
            "tr_id": "TBACC0225R",
        }
        params = {
            "CANO": KIS_ACCOUNT[:8],
            "ACNT_PRDT_CD": KIS_ACCOUNT[8:10],
            "AFHR_FLPR_YN": "",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DCD": "",
            "FUND_SELN_DCD": "",
            "SORT_SQN": "",
            "QUERY_TYPE": "",
            "REAL_YN": "N",
        }
        
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('rt_cd') == '0':
                output2 = data.get('output2', {})
                return float(output2.get('scts_evlu_amt', output2.get('dnca_tot_amt', 0)) or 0)
    except Exception as e:
        print(f"❌ 국내 현물 예수금 조회 실패: {e}")
    return 0.0

def get_kr_stock_info(ticker):
    """종목 정보 조회"""
    try:
        url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/search-info"
        headers = {
            "authorization": f"Bearer {KIS_KEY}",
            "content-type": "application/json; charset=utf-8",
            "tr_id": "CTPF1604R",
        }
        params = {
            "PDNO": ticker,
            "TYPCD": "",
            "INQR_DVSN": ""
        }
        
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('rt_cd') == '0':
                output = data.get('output', {})
                return {
                    'name': output.get('hts_kor_isnm', ''),
                    'price': int(float(output.get('stck_prpr', 0) or 0)),
                }
    except Exception as e:
        print(f"⚠️  {ticker} 정보 조회 실패: {e}")
    
    return None

def get_kr_current_price(ticker):
    """현재가 조회"""
    try:
        url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = {
            "authorization": f"Bearer {KIS_KEY}",
            "content-type": "application/json; charset=utf-8",
            "tr_id": "FHKST01010100",
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
        }
        
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('rt_cd') == '0':
                output = data.get('output', {})
                return int(float(output.get('stck_prpr', 0) or 0))
    except Exception as e:
        print(f"⚠️  {ticker} 현재가 조회 실패: {e}")
    
    return None

def buy_kr_order(ticker, qty, price):
    """국내 주식 시장가 매수 (KIS API)"""
    print(f"\n📤 [{ticker}] 매수 주문 발송...")
    print(f"   수량: {qty}주")
    print(f"   예정가: {price:,}원")
    
    try:
        url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/order-cash"
        headers = {
            "authorization": f"Bearer {KIS_KEY}",
            "content-type": "application/json; charset=utf-8",
            "tr_id": "TTTC0802U",
        }
        
        # 101% 지정가 (시장가 구현)
        order_price = int(price * 1.01)
        
        body = {
            "CANO": KIS_ACCOUNT[:8],
            "ACNT_PRDT_CD": KIS_ACCOUNT[8:10],
            "PDNO": ticker,
            "ORD_DVSN": "01",
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(order_price),
        }
        
        resp = requests.post(url, headers=headers, json=body, timeout=10)
        print(f"   응답 상태: {resp.status_code}")
        
        if resp.status_code == 200:
            data = resp.json()
            print(f"   응답: {data.get('msg1', '')}")
            if data.get('rt_cd') == '0':
                print(f"✅ [국장 매수 체결] {ticker}")
                print(f"   주문가: {order_price:,}원")
                print(f"   수량: {qty}주")
                return True, order_price
            else:
                print(f"❌ 매수 실패: {data.get('msg1', '')}")
                return False, None
    except Exception as e:
        print(f"❌ 매수 주문 실패: {type(e).__name__}: {e}")
        traceback.print_exc()
    
    return False, None

def sell_kr_order(ticker, qty, price):
    """국내 주식 시장가 매도 (KIS API)"""
    print(f"\n📥 [{ticker}] 매도 주문 발송...")
    print(f"   수량: {qty}주")
    print(f"   예정가: {price:,}원")
    
    try:
        url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/order-cash"
        headers = {
            "authorization": f"Bearer {KIS_KEY}",
            "content-type": "application/json; charset=utf-8",
            "tr_id": "TTTC0801U",
        }
        
        # 99% 지정가 (시장가 구현)
        order_price = int(price * 0.99)
        
        body = {
            "CANO": KIS_ACCOUNT[:8],
            "ACNT_PRDT_CD": KIS_ACCOUNT[8:10],
            "PDNO": ticker,
            "ORD_DVSN": "01",
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(order_price),
        }
        
        resp = requests.post(url, headers=headers, json=body, timeout=10)
        print(f"   응답 상태: {resp.status_code}")
        
        if resp.status_code == 200:
            data = resp.json()
            print(f"   응답: {data.get('msg1', '')}")
            if data.get('rt_cd') == '0':
                print(f"✅ [국장 매도 체결] {ticker}")
                print(f"   주문가: {order_price:,}원")
                print(f"   수량: {qty}주")
                return True, order_price
            else:
                print(f"❌ 매도 실패: {data.get('msg1', '')}")
                return False, None
    except Exception as e:
        print(f"❌ 매도 주문 실패: {type(e).__name__}: {e}")
        traceback.print_exc()
    
    return False, None

def find_tradeable_stocks():
    """거래 잘 되는 저가 종목 확인"""
    valid_stocks = []
    print("\n🔍 거래 잘 되는 저가 종목 확인 중...")
    print("-" * 60)
    
    for ticker in CANDIDATE_STOCKS:
        info = get_kr_stock_info(ticker)
        if info:
            price = info.get('price', 0)
            name = info.get('name', ticker)
            valid_stocks.append((ticker, name, price))
            print(f"  ✓ {ticker} {name:20s} {price:8,}원")
            time.sleep(0.3)  # API 속도 제한
    
    return valid_stocks

# =====================================================================
# 메인 테스트
# =====================================================================
def main():
    print("=" * 60)
    print("🇰🇷 국내장 저가 우량주 실시간 매수/매도 테스트")
    print("=" * 60)
    
    if not KIS_KEY or not KIS_SECRET:
        print("❌ KIS 설정이 없습니다!")
        print("   config.json에 kis_key, kis_secret, kis_account를 설정하세요.")
        return
    
    # 1️⃣ 예수금 확인
    print("\n[1단계] 국내장 예수금 확인")
    print("-" * 60)
    kr_cash = get_kr_cash()
    print(f"  국내 현물 예수금: {int(kr_cash):,}원")
    
    if kr_cash < TEST_BUY_AMOUNT:
        print(f"❌ 예수금 부족! (필요: {TEST_BUY_AMOUNT:,}원, 보유: {int(kr_cash):,}원)")
        return
    
    # 2️⃣ 거래 좋은 저가 종목 검색
    print("\n[2단계] 거래 좋은 저가 종목 확인")
    print("-" * 60)
    candidates = find_tradeable_stocks()
    
    if not candidates:
        print("❌ 적절한 종목을 찾을 수 없습니다!")
        return
    
    # 3️⃣ 테스트 대상 선택
    print("\n[3단계] 테스트 대상 선택")
    print("-" * 60)
    print(f"  발견된 종목: {len(candidates)}개")
    target_ticker, target_name, target_price = candidates[0]
    print(f"  선택 종목: {target_ticker} {target_name} ({target_price:,}원)")
    
    # 4️⃣ 매수 수량 계산
    print("\n[4단계] 매수 수량 계산")
    print("-" * 60)
    print(f"  테스트 예산: {TEST_BUY_AMOUNT:,}원")
    print(f"  현재가: {target_price:,}원")
    buy_qty = int(TEST_BUY_AMOUNT / target_price)
    print(f"  계획 수량: {buy_qty}주")
    print(f"  예상 지불: {buy_qty * target_price:,}원")
    
    if buy_qty <= 0:
        print("❌ 매수 수량이 0입니다!")
        return
    
    # 5️⃣ 매수 확인
    print("\n[5단계] 매수 확인")
    print("-" * 60)
    confirm = input(f"  {target_ticker} {target_name} {buy_qty}주를 {target_price:,}원에 매수하시겠습니까? (yes/no): ").strip().lower()
    
    if confirm != "yes":
        print("❌ 매수 취소")
        return
    
    time.sleep(1)
    
    # 6️⃣ 실제 매수
    print("\n[6단계] 실제 매수 실행")
    print("-" * 60)
    buy_ok, buy_price = buy_kr_order(target_ticker, buy_qty, target_price)
    
    if not buy_ok:
        print("❌ 매수 실패!")
        return
    
    time.sleep(3)
    
    # 7️⃣ 가격 모니터링 (30초)
    print("\n[7단계] 가격 모니터링 (30초)")
    print("-" * 60)
    for i in range(6):
        time.sleep(5)
        current_p = get_kr_current_price(target_ticker)
        if current_p:
            profit = (current_p - buy_price) * buy_qty
            profit_rate = ((current_p - buy_price) / buy_price * 100) if buy_price > 0 else 0
            print(f"  {i+1:2d}. 현재가: {current_p:8,}원 | 손익: {profit:+8,}원 | 수익률: {profit_rate:+6.2f}%")
    
    # 8️⃣ 매도 여부 확인
    print("\n[8단계] 매도 확인")
    print("-" * 60)
    final_price = get_kr_current_price(target_ticker)
    if final_price:
        final_profit = (final_price - buy_price) * buy_qty
        final_profit_rate = ((final_price - buy_price) / buy_price * 100) if buy_price > 0 else 0
        print(f"  매수가: {buy_price:,}원")
        print(f"  현재가: {final_price:,}원")
        print(f"  손익: {final_profit:+,}원")
        print(f"  수익률: {final_profit_rate:+.2f}%")
    
    confirm = input(f"  {target_ticker}를 매도하시겠습니까? (yes/no): ").strip().lower()
    
    if confirm != "yes":
        print("⏳ 매도 연기")
        return
    
    # 9️⃣ 실제 매도
    print("\n[9단계] 실제 매도 실행")
    print("-" * 60)
    sell_ok, sell_price = sell_kr_order(target_ticker, buy_qty, final_price)
    
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
        
        print(f"  매수가: {buy_price:,}원 × {buy_qty}주")
        print(f"  매도가: {sell_price:,}원 × {buy_qty}주")
        print(f"  총 손익: {total_profit:+,}원")
        print(f"  수익률: {total_profit_rate:+.2f}%")
        
        if total_profit > 0:
            print(f"\n✅ [테스트 성공] 수익 {total_profit:+,}원 달성!")
        elif total_profit == 0:
            print(f"\n⚪ [테스트 완료] 손익 분기점")
        else:
            print(f"\n⚠️  [테스트 손실] 손실 {total_profit:,}원")

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
