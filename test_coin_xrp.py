"""
🪙 코인 XRP(리플) 실시간 매수/매도 테스트
- 가격 낮은 XRP로 손실 최소화
- main64.py 매수 로직 기반
"""

import sys
import json
import time
import traceback
import warnings
warnings.filterwarnings('ignore')

import pyupbit
import requests

# =====================================================================
# 설정 - config.json에서 로드
# =====================================================================
def load_upbit_config():
    """config.json에서 upbit 인증정보 로드"""
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
            return config.get('upbit_access', ''), config.get('upbit_secret', '')
    except Exception as e:
        print(f"❌ config.json 로드 실패: {e}")
        return '', ''

UPBIT_KEY, UPBIT_SECRET = load_upbit_config()

if not UPBIT_KEY or not UPBIT_SECRET:
    print("❌ 에러: config.json에 upbit_access와 upbit_secret이 없습니다!")
    sys.exit(1)

# 최소 주문 금액 (5500원)
MIN_BUY_AMOUNT = 5500

# 테스트 예산 (적게 설정해서 손실 방어)
TEST_BUY_AMOUNT = 5500  # 5500원만 테스트

# XRP 타겟
TARGET_COIN = "KRW-XRP"

# =====================================================================
# pyupbit 초기화
# =====================================================================
upbit = pyupbit.Upbit(UPBIT_KEY, UPBIT_SECRET)

def get_krw_balance():
    """KRW 보유액 조회"""
    try:
        balances = upbit.get_balances() or []
        krw_bal = float(next((b.get('balance', 0) for b in balances if b.get('currency') == 'KRW'), 0) or 0)
        return krw_bal
    except Exception as e:
        print(f"❌ KRW 잔액 조회 실패: {e}")
        return 0.0

def get_coin_balance(coin_ticker):
    """특정 코인 보유량 조회"""
    try:
        balances = upbit.get_balances() or []
        for b in balances:
            if f"KRW-{b['currency']}" == coin_ticker:
                return float(b.get('balance', 0))
        return 0.0
    except Exception as e:
        print(f"❌ {coin_ticker} 잔액 조회 실패: {e}")
        return 0.0

def get_current_price(ticker):
    """현재가 조회"""
    try:
        price = pyupbit.get_current_price(ticker)
        return float(price) if price else None
    except Exception as e:
        print(f"❌ {ticker} 현재가 조회 실패: {e}")
        return None

def get_ohlcv(ticker, count=250):
    """일봉 OHLCV 조회"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval="day", count=count)
        if df is None or len(df) < 20:
            print(f"⚠️  {ticker}: OHLCV 데이터 부족 (count={len(df) if df is not None else 0})")
            return None
        return df
    except Exception as e:
        print(f"❌ {ticker} OHLCV 조회 실패: {e}")
        return None

# =====================================================================
# 매수 함수
# =====================================================================
def buy_coin_market_order(ticker, amount):
    """시장가 매수"""
    print(f"\n📤 [{ticker}] 매수 주문 발송...")
    print(f"   예산: {amount:,.0f}원")
    
    try:
        resp = upbit.buy_market_order(ticker, amount)
        print(f"   응답: {resp}")
        
        if resp:
            current_p = get_current_price(ticker)
            coin_qty = amount / current_p if current_p and current_p > 0 else 0
            print(f"✅ [코인 매수 체결] {ticker}")
            print(f"   체결가: {current_p:,.0f}원")
            print(f"   수량: {coin_qty:.6f}")
            print(f"   예산: {amount:,.0f}원")
            return True, current_p, coin_qty
        else:
            print(f"❌ 매수 응답 없음")
            return False, None, None
    except Exception as e:
        print(f"❌ 매수 주문 실패: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False, None, None

# =====================================================================
# 매도 함수
# =====================================================================
def sell_coin_market_order(ticker, qty):
    """시장가 매도"""
    print(f"\n📥 [{ticker}] 매도 주문 발송...")
    print(f"   수량: {qty:.6f}")
    
    try:
        resp = upbit.sell_market_order(ticker, qty)
        print(f"   응답: {resp}")
        
        if resp:
            current_p = get_current_price(ticker)
            print(f"✅ [코인 매도 체결] {ticker}")
            print(f"   체결가: {current_p:,.0f}원")
            print(f"   수량: {qty:.6f}")
            return True, current_p
        else:
            print(f"❌ 매도 응답 없음")
            return False, None
    except Exception as e:
        print(f"❌ 매도 주문 실패: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False, None

# =====================================================================
# 메인 테스트
# =====================================================================
def main():
    print("=" * 60)
    print("🪙 XRP 코인 실시간 매수/매도 테스트")
    print("=" * 60)
    
    # 1️⃣ 잔액 확인
    print("\n[1단계] 사전 잔액 확인")
    print("-" * 60)
    krw_before = get_krw_balance()
    xrp_before = get_coin_balance(TARGET_COIN)
    print(f"  KRW 잔액: {krw_before:,.0f}원")
    print(f"  XRP 잔액: {xrp_before:.6f}")
    
    if krw_before < TEST_BUY_AMOUNT:
        print(f"\n❌ KRW 잔액 부족! (필요: {TEST_BUY_AMOUNT:,}원, 보유: {krw_before:,.0f}원)")
        return
    
    # 2️⃣ 현재가 확인
    print("\n[2단계] XRP 현재가 조회")
    print("-" * 60)
    current_p = get_current_price(TARGET_COIN)
    if not current_p:
        print(f"❌ 현재가 조회 실패!")
        return
    print(f"  {TARGET_COIN} 현재가: {current_p:,.0f}원")
    
    # 3️⃣ 기술적 분석 (간단히)
    print("\n[3단계] 기술 분석 (선택사항)")
    print("-" * 60)
    df = get_ohlcv(TARGET_COIN)
    if df is not None:
        print(f"  거래량 데이터: {len(df)}일 보유")
        print(f"  최근 종가: {df.iloc[-1]['close']:,.0f}원")
        print(f"  52주 최고: {df['high'].max():,.0f}원")
        print(f"  52주 최저: {df['low'].min():,.0f}원")
    
    # 4️⃣ 매수 확인
    print("\n[4단계] 매수 확인")
    print("-" * 60)
    print(f"  예정 구매 금액: {TEST_BUY_AMOUNT:,}원")
    print(f"  예정 구매 수량: ~{TEST_BUY_AMOUNT / current_p:.6f}")
    confirm = input("  매수를 진행하시겠습니까? (yes/no): ").strip().lower()
    
    if confirm != "yes":
        print("❌ 매수 취소")
        return
    
    # 5️⃣ 실제 매수
    print("\n[5단계] 실제 매수 실행")
    print("-" * 60)
    buy_ok, buy_price, buy_qty = buy_coin_market_order(TARGET_COIN, TEST_BUY_AMOUNT)
    
    if not buy_ok:
        print("❌ 매수 실패!")
        return
    
    time.sleep(2)
    
    # 6️⃣ 매수 후 잔액 확인
    print("\n[6단계] 매수 후 잔액")
    print("-" * 60)
    krw_after_buy = get_krw_balance()
    xrp_after_buy = get_coin_balance(TARGET_COIN)
    print(f"  KRW 잔액: {krw_after_buy:,.0f}원 (변화: {krw_after_buy - krw_before:,.0f}원)")
    print(f"  XRP 잔액: {xrp_after_buy:.6f} (변화: {xrp_after_buy - xrp_before:+.6f})")
    
    # 7️⃣ 현재가 모니터링 (5초 × 10회 = 50초)
    print("\n[7단계] 가격 모니터링 (50초)")
    print("-" * 60)
    for i in range(10):
        time.sleep(5)
        current_p_now = get_current_price(TARGET_COIN)
        profit_rate = ((current_p_now - buy_price) / buy_price) * 100 if buy_price > 0 else 0
        print(f"  {i+1:2d}. 현재가: {current_p_now:,.0f}원 | 수익률: {profit_rate:+.2f}%")
    
    # 8️⃣ 매도 여부 확인
    print("\n[8단계] 매도 확인")
    print("-" * 60)
    final_price = get_current_price(TARGET_COIN)
    final_profit = ((final_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0
    print(f"  매수가: {buy_price:,.0f}원")
    print(f"  현재가: {final_price:,.0f}원")
    print(f"  수익률: {final_profit:+.2f}%")
    print(f"  예상 수익: {(final_price - buy_price) * xrp_after_buy:+,.0f}원")
    
    confirm = input("  매도를 진행하시겠습니까? (yes/no): ").strip().lower()
    
    if confirm != "yes":
        print("⏳ 매도 연기")
        return
    
    # 9️⃣ 자동 매도 실행
    print("\n[9단계] 실제 매도 실행")
    print("-" * 60)
    sell_ok, sell_price = sell_coin_market_order(TARGET_COIN, xrp_after_buy)
    
    if not sell_ok:
        print("❌ 매도 실패!")
        return
    
    time.sleep(2)
    
    # 🔟 최종 정산
    print("\n[10단계] 최종 정산")
    print("-" * 60)
    krw_final = get_krw_balance()
    xrp_final = get_coin_balance(TARGET_COIN)
    
    total_revenue = krw_final - krw_before
    total_profit_rate = (total_revenue / TEST_BUY_AMOUNT * 100) if TEST_BUY_AMOUNT > 0 else 0
    
    print(f"  초기 KRW: {krw_before:,.0f}원")
    print(f"  최종 KRW: {krw_final:,.0f}원")
    print(f"  손익: {total_revenue:+,.0f}원")
    print(f"  수익률: {total_profit_rate:+.2f}%")
    print(f"  최종 XRP: {xrp_final:.6f}")
    
    if total_revenue > 0:
        print(f"\n✅ [테스트 성공] 수익 {total_revenue:+,.0f}원 달성!")
    elif total_revenue == 0:
        print(f"\n⚪ [테스트 완료] 손익 분기점")
    else:
        print(f"\n⚠️  [테스트 손실] 손실 {total_revenue:,.0f}원")

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
