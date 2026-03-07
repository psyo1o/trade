import json
import mojito
import pprint
from pathlib import Path

# 1. 설정 파일 불러오기
BASE_DIR = Path(__file__).resolve().parent
with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

print("🇺🇸 한국투자증권 미장(해외) 연결 시도 중...")

# 2. 💡 핵심: '서울' 대신 '나스닥'으로 미장 브로커 생성!
broker_us = mojito.KoreaInvestment(
    api_key=config["kis_key"],
    api_secret=config["kis_secret"],
    acc_no=config["kis_account"],
    exchange='나스닥'  
)

# 3. 달러 예수금(해외 잔고) 조회 테스트
try:
    # mojito 라이브러리로 잔고 조회
    balance_us = broker_us.fetch_balance() 
    
    print("\n✅ [미장 통신 성공] 달러 예수금 정보입니다:")
    pprint.pprint(balance_us)
    
except Exception as e:
    print(f"\n❌ [통신 실패] 에러 내용: {e}")