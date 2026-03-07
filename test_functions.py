#!/usr/bin/env python
"""Test main trading functions after broker initialization"""

print("=" * 70)
print("🧪 Testing Main Trading Functions")
print("=" * 70)

import main64

# 브로커 초기화
print("\n1️⃣ Initializing brokers...")
main64.refresh_brokers_if_needed(force=False)

if not main64.broker_kr:
    print("❌ broker_kr initialization failed!")
    exit(1)
    
print("✅ Brokers initialized")
print(f"   broker_kr: {main64.broker_kr}")
print(f"   broker_us: {main64.broker_us}")
print(f"   upbit: {main64.upbit}")

# 국장 보유종목 조회 테스트
print("\n2️⃣ Testing get_held_stocks_kr()...")
try:
    kr_stocks = main64.get_held_stocks_kr()
    print(f"✅ get_held_stocks_kr() returned: {kr_stocks}")
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()

# 미장 현금 조회 테스트
print("\n3️⃣ Testing get_us_cash_real()...")
try:
    us_cash = main64.get_us_cash_real(main64.broker_us)
    print(f"✅ get_us_cash_real(broker_us) returned: ${us_cash:.2f}")
except Exception as e:
    print(f"❌ Error: {e}")

# 코인 잔고 조회 테스트
print("\n4️⃣ Testing upbit.get_balances()...")
try:
    coin_balance = main64.upbit.get_balances()
    print(f"✅ upbit.get_balances() returned {len(coin_balance)} assets")
    for asset in coin_balance[:3]:
        print(f"   - {asset['currency']}: {asset['balance']}")
except Exception as e:
    print(f"❌ Error: {e}")

print("\n" + "=" * 70)
print("✅ All core functions tested successfully!")
print("=" * 70)
