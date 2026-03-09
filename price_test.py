import time
import json
import sys
import os

# kis_auth.py가 같은 폴더에 있다고 가정합니다.
# 만약 다른 경로에 있다면 sys.path.append를 수정해야 합니다.
try:
    import mojito
except ImportError:
    print("🔴 [오류] 'mojito' 라이브러리가 설치되지 않았습니다. pip install mojito-python --upgrade 명령어를 실행해주세요.")
    sys.exit()

# -----------------------------------------------------------------------------
# 이 테스트는 main64.py와 동일한 폴더에 있어야 합니다.
# config.json에 KIS API 키가 올바르게 설정되어 있어야 합니다.
# -----------------------------------------------------------------------------

def initialize_broker():
    """main64.py의 브로커 초기화 로직을 간소화하여 가져옵니다."""
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # main64.py와 동일한 설정값을 사용합니다.
        broker = mojito.KoreaInvestment(
            api_key=config['kis_key'],
            api_secret=config['kis_secret'],
            acc_no=config['kis_account'],
            exchange='서울' 
        )
        print("✅ mojito.KoreaInvestment 브로커 객체 초기화 성공!")
        return broker
    except FileNotFoundError:
        print("🔴 [오류] config.json 파일을 찾을 수 없습니다. API 키 설정 파일을 확인해주세요.")
        sys.exit()
    except Exception as e:
        print(f"🔴 브로커 객체 초기화 실패: {e}")
        sys.exit()

def run_price_check(broker, tickers):
    """지정된 티커 목록의 현재가를 반복적으로 조회하여 출력합니다."""
    print("\n=============================================")
    print("   실시간 현재가 조회 테스트를 시작합니다.   ")
    print("  (5초 간격으로 3회 반복하여 가격 변동 확인)  ")
    print("=============================================\n")

    if not broker:
        print("🔴 브로커가 초기화되지 않아 테스트를 진행할 수 없습니다.")
        return

    for i in range(3): # 3회 반복
        print(f"------ [조회 시도 #{i+1}] ------")
        if not tickers:
            print("조회할 종목 코드가 없습니다.")
            break

        for ticker in tickers:
            print(f"  -> {ticker} 현재가 조회 중...")
            try:
                # main64.py의 핵심 로직과 동일한 API를 호출합니다.
                price_resp = broker.fetch_price(ticker)
                
                if price_resp and price_resp.get('rt_cd') == '0':
                    current_price = price_resp.get('output', {}).get('stck_prpr', 'N/A')
                    print(f"    ✅ 성공: {current_price} 원")
                else:
                    msg = price_resp.get('msg1', '알 수 없는 오류')
                    print(f"    ❌ 실패: API 오류 ({msg})")

            except Exception as e:
                print(f"    ❌ 실패: 예외 발생 ({e})")
        
        if i < 2: # 마지막 시도 후에는 대기하지 않음
            print("\n... 5초 후 다시 조회합니다 ...\n")
            time.sleep(5)

    print("\n=============================================")
    print("         가격 조회 테스트가 종료되었습니다.       ")
    print("=============================================\n")


if __name__ == "__main__":
    # 1. KIS 브로커 초기화
    broker_kr = initialize_broker()
    
    # 2. 테스트할 종목 코드 목록 (로그에 나온 보유 종목)
    #    다른 종목으로 테스트하고 싶으시면 이 리스트를 수정하세요.
    target_tickers = ["031980", "101490", "278470", "319660"]
    
    # 3. 테스트 실행
    run_price_check(broker_kr, target_tickers)