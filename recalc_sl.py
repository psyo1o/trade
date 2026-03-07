"""
"자동복구" 태그가 붙은 종목들의 손절가를
V5.0 전략(매수단가 - 2.5 * ATR)에 맞춰 다시 계산하고 업데이트합니다.
"""
import json
import sys
from pathlib import Path

# main64와 strategy.rules를 임포트하기 위해 경로 추가
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from main64 import load_state, save_state, get_kr_company_name, get_us_company_name
from strategy.rules import get_ohlcv_yfinance
import pandas as pd

STATE_PATH = BASE_DIR / "bot_state.json"

def calculate_atr(ohlcv):
    """주어진 OHLCV 데이터로 ATR(14)을 계산합니다."""
    if not ohlcv or len(ohlcv) < 15:
        return 0
    df = pd.DataFrame(ohlcv)
    df['tr0'] = abs(df['h'] - df['l'])
    df['tr1'] = abs(df['h'] - df['c'].shift())
    df['tr2'] = abs(df['l'] - df['c'].shift())
    df['tr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
    return df['tr'].rolling(14).mean().iloc[-1]

def run_recalculation():
    """손절가 재계산 및 업데이트 실행"""
    print("🤖 손절가 재계산 스크립트를 시작합니다...")
    state = load_state(STATE_PATH)
    if not state or "positions" not in state:
        print("❌ positions 정보가 없거나 bot_state.json 파일을 찾을 수 없습니다.")
        return

    positions = state["positions"]
    update_count = 0

    print("🔍 '자동복구' 태그가 붙은 종목을 검색합니다...")
    for ticker, info in positions.items():
        # 이전에 잘못 계산된 종목도 다시 계산하도록 조건 추가
        if info.get("tier") in ["자동복구(-10%손절)", "자동복구(V5.0손절)"]:
            print(f"\n▶️  '{ticker}' 손절가 재계산 시작...")
            
            # 1. 최신 OHLCV 데이터 가져오기
            ohlcv = get_ohlcv_yfinance(ticker)
            if not ohlcv:
                print(f"  ❌ OHLCV 데이터 조회 실패. 건너뜁니다.")
                continue

            # 2. ATR 및 매수단가 가져오기
            buy_price = info.get('buy_p', 0)
            atr = calculate_atr(ohlcv)
            if atr == 0 or buy_price == 0:
                print(f"  ❌ ATR 또는 매수단가 계산 실패. 건너뜁니다. (ATR: {atr}, 매수단가: {buy_price})")
                continue
                
            # 3. V5.0 기준 새 손절가 계산 (매수단가 기준)
            new_stop_loss = buy_price - (atr * 2.5)
            old_stop_loss = info.get('sl_p', 0)

            # 종목명 가져오기
            name = ""
            if ticker.isdigit(): name = get_kr_company_name(ticker)
            else: name = get_us_company_name(ticker)

            print(f"  - 종목명: {name}")
            print(f"  - 매수단가: {buy_price:,.4f}")
            print(f"  - ATR(14): {atr:,.4f}")
            print(f"  - 이전 손절가: {old_stop_loss:,.4f}")
            print(f"  - 신규 손절가 (매수단가 기준): {new_stop_loss:,.4f}")

            # 4. state 업데이트
            positions[ticker]['sl_p'] = new_stop_loss
            positions[ticker]['tier'] = "자동복구(V5.0손절-매수가)" # 태그 변경
            update_count += 1
            print(f"  ✅ 손절가를 업데이트했습니다.")

    if update_count > 0:
        save_state(STATE_PATH, state)
        print(f"\n✨ 총 {update_count}개 종목의 손절가를 성공적으로 업데이트하고 bot_state.json에 저장했습니다.")
    else:
        print("\nℹ️  손절가를 업데이트할 '자동복구' 종목이 없습니다.")

if __name__ == "__main__":
    run_recalculation()
