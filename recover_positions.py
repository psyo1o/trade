"""실제 계좌 보유종목으로 bot_state.json 복구"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "bot_state.json"

# main64 임포트 및 브로커 초기화
sys.path.insert(0, str(BASE_DIR))
import main64

# 브로커 초기화
print("🔌 브로커 초기화 중...")
main64._create_brokers()
broker_kr = main64.broker_kr
broker_us = main64.broker_us

if not broker_kr or not broker_us:
    print("❌ 브로커 초기화 실패!")
    sys.exit(1)

print("✅ 브로커 준비 완료")

# 국장 보유종목 가져오기
print("🔍 국장 보유종목 조회 중...")
try:
    # main64의 우회 로직 사용
    kr_codes = main64.get_held_stocks_kr()
    bal = main64.get_kr_balance_real(broker_kr)
    
    kr_holdings = {}
    if bal and 'output1' in bal:
        for item in bal['output1']:
            code = item.get('pdno', '')
            qty = int(item.get('hldg_qty', 0))
            avg_price = float(item.get('pchs_avg_pric', 0))
            
            if qty > 0 and avg_price > 0:
                kr_holdings[code] = {
                    'buy_p': avg_price,
                    'sl_p': avg_price * 0.90,  # -10% 손절
                    'max_p': avg_price,
                    'tier': '자동복구(-10%손절)',
                    'qty': qty
                }
                print(f"  ✅ {code}: {avg_price:,.0f}원 × {qty}주")
except Exception as e:
    print(f"⚠️ 국장 조회 실패: {e}")
    import traceback
    traceback.print_exc()
    kr_holdings = {}

# 미장 보유종목 가져오기
print("\n🔍 미장 보유종목 조회 중...")
try:
    from main64 import get_real_us_positions
    bal = get_real_us_positions(broker_us)
    us_holdings = {}
    if bal and 'output1' in bal:
        for item in bal['output1']:
            code = item.get('ovrs_pdno', '')
            qty = float(item.get('ovrs_cblc_qty', 0))
            avg_price = float(item.get('pchs_avg_pric', 0))
            
            if qty > 0 and avg_price > 0:
                us_holdings[code] = {
                    'buy_p': avg_price,
                    'sl_p': avg_price * 0.90,  # -10% 손절
                    'max_p': avg_price,
                    'tier': '자동복구(-10%손절)',
                    'qty': int(qty)
                }
                print(f"  ✅ {code}: ${avg_price:.2f} × {int(qty)}주")
except Exception as e:
    print(f"⚠️ 미장 조회 실패: {e}")
    us_holdings = {}

# bot_state.json 업데이트
print("\n📝 bot_state.json 업데이트 중...")
with open(STATE_PATH, 'r', encoding='utf-8') as f:
    state = json.load(f)

# 기존 positions가 없으면 생성
if 'positions' not in state:
    state['positions'] = {}

# 새로 발견된 종목 추가 (기존 종목 덮어쓰기 방지)
added_count = 0
for code, info in {**kr_holdings, **us_holdings}.items():
    if code not in state['positions']:
        qty = info.pop('qty')  # qty는 저장 안 함
        state['positions'][code] = info
        print(f"  ➕ {code} 추가됨 (평단: {info['buy_p']})")
        added_count += 1
    else:
        print(f"  ⏭️ {code} 이미 존재 (건너뜀)")

# 저장
with open(STATE_PATH, 'w', encoding='utf-8') as f:
    json.dump(state, f, indent=2, ensure_ascii=False)

print(f"\n✅ 복구 완료! {added_count}개 종목 추가됨")
print(f"📊 현재 총 보유: {len(state['positions'])}개 종목")
