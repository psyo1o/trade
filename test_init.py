#!/usr/bin/env python
"""Test broker initialization"""

print("=" * 60)
print("🧪 Testing Broker Initialization")
print("=" * 60)

try:
    print("\n1️⃣ Importing main64...")
    import main64
    print("   ✅ main64 imported successfully")
    
    print("\n2️⃣ Checking broker objects...")
    print(f"   broker_kr = {main64.broker_kr}")
    print(f"   broker_us = {main64.broker_us}")
    print(f"   upbit = {main64.upbit}")
    
    print("\n3️⃣ Calling refresh_brokers_if_needed(force=False)...")
    main64.refresh_brokers_if_needed(force=False)
    print("   ✅ refresh_brokers_if_needed completed")
    
    print("\n4️⃣ Checking broker objects after refresh...")
    print(f"   broker_kr = {main64.broker_kr}")
    print(f"   broker_us = {main64.broker_us}")
    print(f"   upbit = {main64.upbit}")
    
    if main64.broker_kr and main64.broker_us and main64.upbit:
        print("\n✅ SUCCESS: All brokers initialized!")
    else:
        print("\n❌ FAIL: Some brokers are still None")
        
except Exception as e:
    print(f"\n❌ ERROR: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 60)
