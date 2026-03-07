"""실제 보유종목 확인"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import main64

# 브로커 초기화
print("🔌 브로커 초기화 중...")
main64._create_brokers()

# 국장 보유종목
print("\n🇰🇷 국장 보유종목 조회 중...")
try:
    kr_codes = main64.get_held_stocks_kr()
    print(f"  ✅ 국장 종목 코드 리스트: {kr_codes}")
    print(f"  📊 국장 보유 종목 수: {len(kr_codes)}개")
    
    # 상세 정보
    kr_info = main64.get_held_stocks_kr_info()
    if kr_info:
        print(f"\n  📋 국장 상세:")
        for item in kr_info:
            print(f"    - {item['name']}({item['code']}): {item['qty']}주")
except Exception as e:
    print(f"  ❌ 국장 조회 실패: {e}")
    import traceback
    traceback.print_exc()

# 미장 보유종목
print("\n🇺🇸 미장 보유종목 조회 중...")
try:
    us_codes = main64.get_held_stocks_us()
    print(f"  ✅ 미장 종목 코드 리스트: {us_codes}")
    print(f"  📊 미장 보유 종목 수: {len(us_codes)}개")
    
    # 상세 정보
    us_info = main64.get_held_stocks_us_info()
    if us_info:
        print(f"\n  📋 미장 상세:")
        for item in us_info:
            print(f"    - {item['name']}({item['code']}): {item['qty']}주")
except Exception as e:
    print(f"  ❌ 미장 조회 실패: {e}")
    import traceback
    traceback.print_exc()

# 코인 보유종목
print("\n🪙 코인 보유종목 조회 중...")
try:
    coin_codes = main64.get_held_coins()
    print(f"  ✅ 코인 종목 리스트: {coin_codes}")
    print(f"  📊 코인 보유: {len(coin_codes)}개")
except Exception as e:
    print(f"  ❌ 코인 조회 실패: {e}")

print(f"\n{'='*60}")
print(f"📊 총계: 국장 {len(kr_codes) if 'kr_codes' in locals() else 0}개 / 미장 {len(us_codes) if 'us_codes' in locals() else 0}개 / 코인 {len(coin_codes) if 'coin_codes' in locals() else 0}개")
print(f"{'='*60}")
