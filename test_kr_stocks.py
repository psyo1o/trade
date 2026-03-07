#!/usr/bin/env python
"""Debug get_held_stocks_kr with detailed output"""
import main64

main64.refresh_brokers_if_needed(force=False)

print(" Testing get_held_stocks_kr()...")
stocks = main64.get_held_stocks_kr()
print(f"Result: {stocks}")
