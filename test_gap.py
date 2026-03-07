# test_gap.py
import json
from pathlib import Path
import mojito

# 1. 설정 파일 로드
BASE_DIR = Path(__file__).resolve().parent
with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

# 2. 국장 브로커 연결
broker_kr = mojito.KoreaInvestment(
    api_key=config["kis_key"],
    api_secret=config["kis_secret"],
    acc_no=config["kis_account"],
    exchange='서울'
)

# 3. 에러 안 나게 수정한 가벼운 데이터 수집 함수 (count 옵션 제거!)
def get_kis_ohlcv_test(broker, code):
    try:
        # 💡 문제의 count=250 옵션을 빼서 에러를 해결!
        resp = broker.fetch_ohlcv(code, timeframe='D', adj_price=True)
        if not resp or 'output2' not in resp: 
            return []
        
        rows = []
        for item in reversed(resp['output2']):
            rows.append({
                'o': float(item['stck_oprc']), 
                'h': float(item['stck_hgpr']),
                'l': float(item['stck_lwpr']), 
                'c': float(item['stck_clpr']), 
                'v': float(item['acml_vol'])
            })
        return rows
    except Exception as e: 
        print(f"데이터 긁기 에러: {e}")
        return []

# --- 🚀 본격적인 테스트 시작 ---
print("=== 🛡️ 실제 API 데이터 기반 갭상승 컷오프 테스트 (에러 수정판) ===")

test_targets = ["005930", "000660"] 

for t in test_targets:
    print(f"\n[{t}] 데이터 분석 중...")
    ohlcv = get_kis_ohlcv_test(broker_kr, t)
    
    if len(ohlcv) < 2:
        print(f"  ❌ [{t}] 데이터를 못 가져왔습니다.")
        continue

    try:
        prev_close = float(ohlcv[-2]['c']) # 어제 종가
        today_open = float(ohlcv[-1]['o']) # 오늘 시가
        
        gap_up_rate = ((today_open - prev_close) / prev_close) * 100
        
        print(f"  - 어제 종가: {prev_close:,.0f}원")
        print(f"  - 오늘 시가: {today_open:,.0f}원")
        print(f"  - 계산된 갭상승률: {gap_up_rate:.2f}%")
        
        if gap_up_rate >= 5.0:
            print(f"  - 🛡️ 결과: 5% 이상! 매수 패스 ⛔ (방패 정상 작동)")
        else:
            print(f"  - ✅ 결과: 5% 미만! 정상 매수 로직 진입 🟢")

    except Exception as e:
         print(f"  ❌ 계산 중 에러 발생: {e}")

print("\n=== 테스트 종료 ===")