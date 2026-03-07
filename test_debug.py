#!/usr/bin/env python
"""Debug tr_cont error"""
import main64

main64.refresh_brokers_if_needed(force=False)

print("Testing broker_kr.fetch_balance()...")
try:
    bal = main64.broker_kr.fetch_balance()
    print(f"Success: {bal}")
except KeyError as e:
    print(f"KeyError: {e}")
    print(f"Error type: {type(e)}")
    print(f"Error str: '{str(e)}'")
    target = "'tr_cont'"
    print(f"Comparison: {str(e) == target}")
except Exception as e:
    print(f"Other error: {e}, type: {type(e)}")
