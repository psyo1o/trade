# -*- coding: utf-8 -*-
"""
한국투자 API 기반 **야간/장후 스크리너** — 당일 거래대금·시총 상위 후보를 뽑아 JSON에 저장.

실행
    * ``run_bot.start_scanner_scheduler`` 가 거래일 **15:00 KST** 에 ``run_night_screener`` 를 호출.
    * 단독 테스트 시 이 파일을 직접 실행해도 된다 (``config.json``·``kis_hts_id`` 필요).

토큰
    * ``kis_token.json`` — ``run_bot`` 과 호환되는 ``access_token`` + ``timestamp`` 형식을 사용한다.
"""
import json, requests, time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

appkey = config["kis_key"]
appsecret = config["kis_secret"]
hts_id = config.get("kis_hts_id", "").strip()

if not hts_id:
    print("🚨 config.json에 'kis_hts_id'를 입력해주세요!")
    exit()

def get_fresh_token():
    token_file = BASE_DIR / "kis_token.json"
    
    if token_file.exists():
        with open(token_file, "r") as f:
            try:
                saved = json.load(f)
                # run_bot.py 형식 (access_token) 또는 screener.py 형식 (token) 지원
                token = saved.get("access_token") or saved.get("token")
                timestamp = saved.get("timestamp", 0)
                # 11시간 50분 이내라면 기존 토큰 사용
                if time.time() - timestamp < 11.83 * 3600:
                    return token
            except:
                pass

    print("🔑 한투 서버에서 보안 토큰 확인 중...")
    url = "https://openapi.koreainvestment.com:9443/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": appkey,
        "appsecret": appsecret
    }
    res = requests.post(url, json=body)
    data = res.json()
    
    if "access_token" in data:
        token = data["access_token"]
        # run_bot.py와 동일한 형식으로 저장
        data['timestamp'] = time.time()
        with open(token_file, "w") as f:
            json.dump(data, f)
        return token
    else:
        print(f"🚨 토큰 발급 실패: {data}")
        exit()

ACCESS_TOKEN = get_fresh_token()

def fetch_hts_conditions():
    url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/psearch-title"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {ACCESS_TOKEN}",
        "appkey": appkey,
        "appsecret": appsecret,
        "tr_id": "HHKST03900300",
        "custtype": "P"
    }
    params = {"user_id": hts_id}
    res = requests.get(url, headers=headers, params=params)
    return res.json()

def get_condition_stocks(seq):
    url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/psearch-result"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {ACCESS_TOKEN}",
        "appkey": appkey,
        "appsecret": appsecret,
        "tr_id": "HHKST03900400",
        "custtype": "P"
    }
    params = {
        "user_id": hts_id,
        "seq": seq
    }
    res = requests.get(url, headers=headers, params=params)
    return res.json()

def run_night_screener():
    print(f"🌙 [야간 발굴기] HTS({hts_id}) 조건검색 연동을 시작합니다...")
    cond_list_data = fetch_hts_conditions()
    
    if 'output2' not in cond_list_data:
        print("\n⚠️ [에러 발생] 조건식 목록을 가져오지 못했습니다.")
        print(f"👉 서버 원본 응답: {cond_list_data}")
        return
        
    target_stocks = []
    
    for cond in cond_list_data['output2']:
        # 💡 [해결!] 한투 서버의 요상한 이름표(condition_nm)에 완벽 대응!
        seq = cond.get('seq', cond.get('SEQ', ''))
        name = cond.get('condition_nm', cond.get('CONDITION_NM', '이름모름'))
        
        print(f"  -> 🔍 [{name}] (번호:{seq}) 조건식 스캔 중...")
        
        data = get_condition_stocks(seq)
        
        if data.get('rt_cd') == '0' and 'output2' in data:
            stocks = [item['code'] for item in data['output2']]
            print(f"     ✅ {len(stocks)}개 종목 포착 완료!")
            target_stocks.extend(stocks)
        else:
            print(f"     ❌ 종목을 가져오지 못했습니다. 응답: {data}")
            
        time.sleep(0.5) 
        
    target_stocks = list(set(target_stocks))
    
    with open(BASE_DIR / "kr_targets.json", "w", encoding="utf-8") as f:
        json.dump(target_stocks, f)
        
    print(f"\n🎉 [발굴 완료] 총 {len(target_stocks)}개의 최정예 타겟이 kr_targets.json에 꽂혔습니다!")

if __name__ == "__main__":
    run_night_screener()