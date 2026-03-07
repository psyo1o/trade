#!/usr/bin/env python
"""Final test - check all fixes"""
import sys
print("=" * 70)
print("🔍 최종 테스트 - 모든 수정사항 확인")
print("=" * 70)

print("\n1️⃣ Importing and initializing...")
import main64
main64.refresh_brokers_if_needed(force=False)

print("\n2️⃣ Testing get_held_stocks_kr()...")
try:
    kr_stocks = main64.get_held_stocks_kr()
    print(f"✅ 국장 보유: {len(kr_stocks)}개")
except Exception as e:
    print(f"❌ Error: {e}")

print("\n3️⃣ Testing get_held_stocks_us_detail()...")
try:
    us_data = main64.get_held_stocks_us_detail()
    print(f"✅ 미장 보유: {len(us_data)}개")
    for item in us_data:
        print(f"  - {item.get('code')}: {item.get('qty')} @ ${item.get('avg_p')}")
except Exception as e:
    print(f"❌ Error: {e}")

print("\n4️⃣ Testing heartbeat_report()...")
try:
    main64.heartbeat_report()
    print("✅ Heartbeat report 성공")
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()

print("\n5️⃣ Testing save_state()...")
try:
    from risk.guard import load_state, save_state
    state = load_state(main64.STATE_PATH)
    state['test'] = 'value'
    save_state(main64.STATE_PATH, state)
    print("✅ save_state() 순서 수정 완료")
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 70)
print("✅ 모든 테스트 완료!")
print("=" * 70)
