#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
max_p 업데이트 간단 테스트 (현재 상태 확인)
"""

import json
import requests

# bot_state.json 로드
with open("bot_state.json", "r", encoding="utf-8") as f:
    state = json.load(f)

print("=" * 80)
print("📊 max_p 업데이트 로직 테스트 (현재 상태 확인)")
print("=" * 80)

# 현재 보유 종목 분석
positions = state.get("positions", {})

if not positions:
    print("\n❌ 보유 포지션이 없습니다.")
else:
    print(f"\n총 보유 포지션: {len(positions)}개")
    
    for ticker, info in sorted(positions.items()):
        buy_p = info.get('buy_p', 0)
        max_p = info.get('max_p', buy_p)
        sl_p = info.get('sl_p', 0)
        tier = info.get('tier', 'N/A')
        buy_time = info.get('buy_time', 0)
        
        # 수익률 계산
        if max_p > 0 and buy_p > 0:
            profit_rate = ((max_p - buy_p) / buy_p) * 100
        else:
            profit_rate = 0.0
        
        # max_p 업데이트 여부 판단
        if max_p > buy_p:
            status = "✅ max_p 이상으로 상승됨"
        else:
            status = "⚠️  max_p = 매수가 (아직 상승 안 함)"
        
        print(f"\n  📌 {ticker}")
        print(f"     매수가: {buy_p:,.2f}")
        print(f"     최고가: {max_p:,.2f}")
        print(f"     손절가: {sl_p:,.2f}")
        print(f"     수익률(매수→최고): {profit_rate:+.2f}%")
        print(f"     상태: {status}")
        print(f"     등급: {tier}")

print("\n" + "=" * 80)
print("📋 테스트 결과 분석")
print("=" * 80)

# 통계
total = len(positions)
risen = sum(1 for info in positions.values() if info.get('max_p', 0) > info.get('buy_p', 0))
not_risen = total - risen

print(f"\n총 포지션: {total}개")
print(f"  ✅ 최고가가 매수가보다 높은 종목: {risen}개")
print(f"  ⚠️  최고가 = 매수가 (상승 안 한 종목): {not_risen}개")

if not_risen > 0:
    print(f"\n⚠️  {not_risen}개 종목은 아직 현재가가 매수가에 도달하지 못한 상태입니다.")
    print(f"   다음 15분 주기(매도 루프) 실행 시 높은 가격으로 업데이트될 예정입니다.")
else:
    print(f"\n✅ 모든 종목의 최고가가 정상적으로 기록되고 있습니다!")

print("\n" + "=" * 80)
print("ℹ️  봇이 재시작된 후부터 실시간으로 max_p가 업데이트됩니다.")
print("    로그에서 '📈 [종목] max_p 업데이트' 메시지를 확인하세요.")
print("=" * 80)
