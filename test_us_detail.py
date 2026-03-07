#!/usr/bin/env python
"""Test US detail function"""
import main64

main64.refresh_brokers_if_needed(force=False)
print('\nTesting get_held_stocks_us_detail()...')
us_data = main64.get_held_stocks_us_detail()
print(f'Result: {us_data}')
print(f'Length: {len(us_data)}')

for item in us_data:
    print(f"  Code: {item.get('code')}, Qty: {item.get('qty')}, AvgP: {item.get('avg_p')}")
