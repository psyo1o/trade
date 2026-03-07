import time, json, schedule, pyupbit, requests, traceback, atexit
import mojito
import pytz
from pathlib import Path
from datetime import datetime
import screener
import threading
from risk.guard import load_state, save_state, in_cooldown, set_cooldown, can_open_new
from strategy.rules import get_ohlcv_yfinance, get_ohlcv_realtime, calculate_pro_signals, check_pro_exit
import yfinance as yf
import pandas as pd
import pandas_market_calendars as mcal
KIS_TOKEN = None
def get_held_stocks_kr():
    """🇰🇷 국장 실제 보유 종목 코드 리스트 가져오기"""
    try:
        # broker_kr은 메인 로직에서 생성된 객체를 사용한다고 가정
        bal = broker_kr.fetch_balance()
        if not bal or 'output1' not in bal: return []
        return [s['pdno'] for s in bal['output1'] if int(s['hldg_qty']) > 0]
    except Exception as e:
        print(f"⚠️ 국장 잔고 조회 실패: {e}")
        return []

def get_held_stocks_us():
    """🇺🇸 미장 실제 보유 종목 티커 리스트 가져오기"""
    try:
        bal = get_real_us_positions(broker_us) # 💡 직접 만든 무적의 스캐너로 교체!
        if not bal or 'output1' not in bal: return []
        # 💡 미장의 진짜 수량 단어인 'ovrs_cblc_qty' 사용!
        return [s['ovrs_pdno'] for s in bal['output1'] if float(s.get('ovrs_cblc_qty', 0)) > 0]
    except Exception as e:
        return []

def get_held_coins():
    """🪙 코인 실제 보유 티커 리스트 가져오기"""
    try:
        balances = upbit.get_balances()
        return [f"KRW-{b['currency']}" for b in balances if b['currency'] not in ['KRW', 'VTHO']]
    except Exception as e:
        print(f"⚠️ 코인 잔고 조회 실패: {e}")
        return []

def get_kis_market_cap_rank(broker, limit=100):
    """[전문가용] 시가총액 상위 N개 종목 공수"""
    import requests
    try:
        url = f"{broker.base_url}/uapi/domestic-stock/v1/quotations/market-cap-rank"
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {broker.access_token}",
            "appkey": broker.api_key, "appsecret": broker.api_secret,
            "tr_id": "FHPST01740000", "custtype": "P"
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "20174",
            "FID_DIV_CLS_CODE": "0", "FID_INPUT_ISCD": "0000", "FID_TRGT_CLS_CODE": "0",
            "FID_TRGT_EXLS_CLS_CODE": "0", "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "", "FID_VOL_CNT": ""
        }
        res = requests.get(url, headers=headers, params=params)
        data = res.json()
        if 'output' in data:
            return [item['mksc_shrn_iscd'] for item in data['output'][:limit]]
    except: pass
    return []

# 🛡️ [대장주 보호막] 봇이 절대 건드리지 않는 종목 (티커/코드)
# 대표님이 직접 사서 존버하실 종목들을 여기에 계속 추가하세요.
CORE_ASSETS = ["005930", "000660", "QQQ", "NVDA", "TSLA", "AAPL", "MSFT"]

# =====================================================================
# 🛡️ [MDD 브레이크 시스템] 국장 & 미장 통합 관리
# =====================================================================
def check_mdd_break(market_type, current_equity, state):
    """실시간 자산을 기준으로 고점 대비 5% 하락 시 매수 중단 로직"""
    peak_key = f"peak_equity_{market_type}"
    peak_equity = state.get(peak_key, current_equity)
    
    if current_equity > peak_equity:
        state[peak_key] = current_equity # 고점 갱신
        save_state(STATE_PATH, state)
        return True
    
    if current_equity < peak_equity * 0.95:
        print(f"  -> 🚨 [{market_type}] MDD 브레이크 발동! (고점 대비 -5% 하락). 신규 매수 차단.")
        return False
    return True

import requests

def safe_get(data, key, default=None):
    """데이터가 딕셔너리일 때만 .get()을 호출합니다."""
    if isinstance(data, dict):
        return data.get(key, default)
    return default

def ensure_dict(data):
    """API 응답이 list로 잘못 올 경우 빈 dict로 안전하게 교체합니다."""
    return data if isinstance(data, dict) else {}

# ---------------------------------------------------------
# 1. 설정 파일 로드 (config.json에 kis_key, kis_secret 등 입력 필수)
# ---------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

# ---------------------------------------------------------
# 2. 64비트 직통 브로커 연결 (키움 32비트 브릿지 완벽 대체)
# ---------------------------------------------------------
# 🇰🇷 국장 전용 브로커
broker_kr = mojito.KoreaInvestment(
    api_key=config["kis_key"],
    api_secret=config["kis_secret"],
    acc_no=config["kis_account"],
    exchange='서울'
)
time.sleep(2)
# 🇺🇸 미장 전용 브로커
broker_us = mojito.KoreaInvestment(
    api_key=config["kis_key"],
    api_secret=config["kis_secret"],
    acc_no=config["kis_account"],
    exchange='나스닥'
)

# 🪙 코인 전용 브로커
upbit = pyupbit.Upbit(config["upbit_access"], config["upbit_secret"])

# ---------------------------------------------------------
# 3. 텔레그램 및 안전장치 (기존 로직 100% 보존)
# ---------------------------------------------------------
def send_telegram(message):
    url = f"https://api.telegram.org/bot{config['telegram_token']}/sendMessage"
    params = {"chat_id": config['telegram_chat_id'], "text": message}
    try: requests.post(url, params=params, timeout=10)
    except: print("⚠️ 텔레그램 전송 실패")

def shutdown_handler():
    err = traceback.format_exc()
    if "SystemExit" in err or "KeyboardInterrupt" in err:
        send_telegram("⚠️ [알림] 봇이 수동으로 종료되었습니다.")
    elif "NoneType: None" not in err:
        err_message = repr(err[-200:]) # err 내용을 안전한 문자열로 변환
        send_telegram(f"🚨 [긴급] 봇 에러 발생:\n{err_message}") # repr() 적용

    # print 문을 제거하여 SyntaxError를 근본적으로 방지합니다.
    # 오류 보고는 send_telegram을 통해 이루어집니다.

atexit.register(shutdown_handler)

    # ==========================================================
    # 💡 1. 잃어버린 고아 종목 구출 (MTS 수동매수 감지 후 장부 등록)
    # ==========================================================
    # 국장 구출
    for t in held_kr:
        if t not in current_positions:
            print(f"  -> 🚨 [상태 복구] 국장 '{t}' 강제 등록 (수동매수 감지)")
            # 임시로 현재가를 0으로 세팅 (다음 루프 때 진짜 가격으로 업데이트 됨)
            current_positions[t] = {'buy_p': 0, 'sl_p': 0, 'max_p': 0, 'tier': "수동/복구"}
            changes_made = True
            
    # 미장 구출
    for t in held_us:
        if t not in current_positions:
            print(f"  -> 🚨 [상태 복구] 미장 '{t}' 강제 등록 (수동매수 감지)")
            current_positions[t] = {'buy_p': 0, 'sl_p': 0, 'max_p': 0, 'tier': "수동/복구"}
            changes_made = True
            
    # 코인 구출
    for t in held_coins:
        if t not in current_positions:
            print(f"  -> 🚨 [상태 복구] 코인 '{t}' 강제 등록 (수동매수 감지)")
            current_positions[t] = {'buy_p': 0, 'sl_p': 0, 'max_p': 0, 'tier': "수동/복구"}
            changes_made = True

    # ==========================================================
    # 🧹 2. 유령 종목 삭제 (기존 네가 짠 완벽한 로직 그대로!)
    # ==========================================================
    for ticker in list(current_positions.keys()):
        # 1. 국장 (숫자로만 된 코드)
        if ticker.isdigit():
            if ticker not in held_kr:
                to_delete.append(ticker)
        
        # 2. 코인 (KRW- 로 시작하는 티커)
        elif ticker.startswith("KRW-"):
            if ticker not in held_coins:
                to_delete.append(ticker)
        
        # 3. 미장 (나머지 영어 티커)
        else:
            if ticker not in held_us:
                to_delete.append(ticker)

    if changes_made:
        save_state(STATE_PATH, state)
        if to_delete:
            print(f"  -> ✅ 총 {len(to_delete)}개의 유령 종목 정리 완료.")
        return True
    else:
        # 💡 2. 변경사항이 없을 때도 안심시켜 주는 보고 추가!
        print("  -> ✅ 계좌와 장부가 완벽히 일치합니다. (변경 없음)")
        return False

    # ==========================================================
    # 💾 3. 변경사항 저장
    # ==========================================================
    if changes_made:
        save_state(STATE_PATH, state)
        if to_delete:
            print(f"  -> ✅ 총 {len(to_delete)}개의 유령 종목 정리 완료. (남은 종목: {len(state['positions'])}개)")
        return True
    return False
# ---------------------------------------------------------
# 4. 생존 신고 (3개 시장 자산 동시 확인)
# ---------------------------------------------------------
def heartbeat_report():
    try:
        # 국장 원화 예수금 조회 (에러 시 0원 처리)
        kr_resp = ensure_dict(broker_kr.fetch_balance())
        kr_cash = int(kr_resp.get('output2', [{}])[0].get('dnca_tot_amt', 0)) if kr_resp.get('output2') else 0
        
        # 미장 달러 예수금 조회
        us_resp = ensure_dict(broker_us.fetch_balance())
        us_cash = float(us_resp.get('output2', {}).get('frcr_buy_amt_smtl1', 0.0))
        
        # 코인 예수금 조회
        krw_bal = int(upbit.get_balance("KRW") or 0)
        
        msg = f"💓 [3콤보 생존신고]\n🇰🇷 국장 예수금: {kr_cash:,}원\n🇺🇸 미장 예수금: ${us_cash:.2f}\n🪙 코인 예수금: {krw_bal:,}원"
        send_telegram(msg)
    except Exception as e:
        print(f"⚠️ 보고 에러: {e}")

# ---------------------------------------------------------
# 5. 메인 매매 사이클
# ---------------------------------------------------------
import numpy as np

from datetime import datetime, timedelta
def sync_all_positions(state, held_kr, held_us, held_coins):
    """국장/미장/코인 통합 장부 정리 시스템"""
    if "positions" not in state:
        return False

    initial_count = len(state["positions"])
    current_positions = state["positions"]
    to_delete = []

    for ticker in list(current_positions.keys()):
        # 1. 국장 (숫자로만 된 코드)
        if ticker.isdigit():
            if ticker not in held_kr:
                to_delete.append(ticker)
        
        # 2. 코인 (KRW- 로 시작하는 티커)
        elif ticker.startswith("KRW-"):
            if ticker not in held_coins:
                to_delete.append(ticker)
        
        # 3. 미장 (나머지 영어 티커)
        else:
            if ticker not in held_us:
                to_delete.append(ticker)

    # 유령 종목 일괄 삭제
    for t in to_delete:
        print(f"  -> 🧹 [통합 장부정리] 계좌에 없는 '{t}' 발견! 메모장에서 삭제했습니다.")
        del state["positions"][t]

    if to_delete:
        save_state(STATE_PATH, state)
        print(f"  -> ✅ 총 {len(to_delete)}개의 유령 종목을 정리했습니다. (남은 종목: {len(state['positions'])}개)")
        return True
    return False
# =====================================================================
# 🧠 [V3.0 통합 기상청 봇] : 실시간 데이터를 기반으로 날씨를 판독합니다.
# =====================================================================
def get_real_weather(broker_kr, broker_us):
    """V5.0 기관용 기상청: 20일선 기준 ±0.5% 밴드로 횡보/대세 완벽 구분"""
    weather = {"KR": "☁️ SIDEWAYS", "US": "☁️ SIDEWAYS", "COIN": "☁️ SIDEWAYS"}
    
    # 🇰🇷 1. 국장 날씨 (KODEX 200 - 069500)
    try:
        ohlcv = get_kis_ohlcv(broker_kr, "069500")
        if ohlcv and len(ohlcv) >= 20:
            closes = [float(x['c']) for x in ohlcv if 'c' in x]
            current = closes[-1]
            ma20 = sum(closes[-20:]) / 20
            
            # 💡 [핵심] 20일선 대비 ±0.5%를 기준으로 완벽하게 3등분 합니다.
            if current > ma20 * 1.005:      # 20일선 위로 0.5% 이상 돌파 (확실한 불장)
                weather['KR'] = "☀️ BULL"
            elif current < ma20 * 0.995:    # 20일선 아래로 0.5% 이상 이탈 (확실한 하락장)
                weather['KR'] = "🌧️ BEAR"
            else:                           # 그 사이에서 비비적거림 (이게 진짜 횡보장!)
                weather['KR'] = "☁️ SIDEWAYS"
    except: pass
    
    # 🇺🇸 2. 미장 날씨 (SPY)
    try:
        resp = broker_us.fetch_ohlcv("SPY", timeframe='D', adj_price=True)
        if resp and 'output2' in resp:
            closes = [float(x['ovrs_nmix_prpr']) for x in reversed(resp['output2'])]
            if len(closes) >= 20:
                current = closes[-1]
                ma20 = sum(closes[-20:]) / 20
                
                if current > ma20 * 1.005:
                    weather['US'] = "☀️ BULL"
                elif current < ma20 * 0.995:
                    weather['US'] = "🌧️ BEAR"
                else:
                    weather['US'] = "☁️ SIDEWAYS"
    except: pass
    
    # 🪙 3. 코인 날씨 (비트코인)
    try:
        import pyupbit
        df = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=20)
        if df is not None and len(df) >= 20:
            current = df['close'].iloc[-1]
            ma20 = df['close'].mean()
            
            # 코인은 변동성이 크므로 ±1%를 횡보 박스권으로 잡습니다.
            if current > ma20 * 1.01:
                weather['COIN'] = "☀️ BULL"
            elif current < ma20 * 0.99:
                weather['COIN'] = "🌧️ BEAR"
            else:
                weather['COIN'] = "☁️ SIDEWAYS"
    except: pass
    
    return weather

# =====================================================================
# 🕒 시계탑: 캘린더 로드 (봇 메모리 최적화를 위해 전역에 한 번만 선언)
# =====================================================================
krx_cal = mcal.get_calendar('XKRX')  # 한국거래소
nyse_cal = mcal.get_calendar('NYSE') # 뉴욕증권거래소 (나스닥 등 미국장 전체 통용)

def is_market_open(market="KR"):
    if market == "COIN": 
        return True
        
    # 모든 시간 비교를 절대 기준인 UTC로 통일하여 꼬임을 방지합니다.
    now_utc = pd.Timestamp.now(tz='UTC')
    
    # 1. 시장별 캘린더 및 현지 시간대 설정
    if market == "KR":
        cal = krx_cal
        now_local = now_utc.astimezone(pytz.timezone('Asia/Seoul'))
    elif market == "US":
        cal = nyse_cal
        now_local = now_utc.astimezone(pytz.timezone('US/Eastern'))
    else:
        return False
        
    # 2. 현지 기준 '오늘' 날짜의 달력 스케줄을 가져옴
    today_str = now_local.strftime('%Y-%m-%d')
    schedule = cal.schedule(start_date=today_str, end_date=today_str)
    
    # 3. 스케줄이 비어있다면 = 주말이거나 공휴일로 '휴장'
    if schedule.empty:
        return False
        
    # 4. 오늘의 정확한 개장/폐장 시간 가져오기 (schedule은 기본적으로 UTC 시간을 반환함)
    market_open = schedule.iloc[0]['market_open']
    market_close = schedule.iloc[0]['market_close']
    
    # 5. 현재 시간이 개장 시간과 폐장 시간 사이에 있는지 확인
    return market_open <= now_utc <= market_close
    
def get_us_cash_real(broker):
    global KIS_TOKEN
    base_url = getattr(broker, "base_url", "https://openapi.koreainvestment.com:9443")
    is_mock = "vps" in base_url or "vts" in base_url
    if not KIS_TOKEN:
        try:
            auth_url = f"{base_url}/oauth2/tokenP"
            body = {"grant_type": "client_credentials", "appkey": config["kis_key"], "appsecret": config["kis_secret"]}
            res = requests.post(auth_url, json=body)
            KIS_TOKEN = res.json().get("access_token")
        except Exception as e: print(f"⚠️ 직통 토큰 발급 실패: {e}")

    try:
        tr_id = "VTTT3007R" if is_mock else "JTTT3007R"
        url = f"{base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
        headers = {
            "content-type": "application/json", "authorization": f"Bearer {KIS_TOKEN}",
            "appkey": config["kis_key"], "appsecret": config["kis_secret"],
            "tr_id": tr_id, "custtype": "P"
        }
        params = {
            "CANO": broker.acc_no.split("-")[0], "ACNT_PRDT_CD": broker.acc_no.split("-")[1],
            "OVRS_EXCG_CD": "NASD", "OVDV_CSHN_VALD_YN": "N", "ITEM_CD": "AAPL", "OVRS_ORD_UNPR": "0"
        }
        res = requests.get(url, headers=headers, params=params)
        data = res.json()
        if 'output' in data:
            out = data['output']
            amt = float(out.get('ovrs_ord_psbl_amt', 0.0))
            if amt == 0.0: amt = float(out.get('frcr_ord_psbl_amt1', 0.0))
            return amt
        return 0.0
    except Exception as e: return 0.0
# =====================================================================
# 📖 [번역기] 종목 코드를 한글 이름으로 바꿔줍니다.
# =====================================================================
kr_name_dict = {"005930": "삼성전자", "000660": "SK하이닉스", "035420": "NAVER", "035720": "카카오", "005380": "현대차", "069500": "KODEX 200"}
us_name_dict = {"AAPL": "애플", "MSFT": "마이크로소프트", "NVDA": "엔비디아", "TSLA": "테슬라", "AMZN": "아마존"}

STATE_PATH = BASE_DIR / "bot_state.json"
# ---------------------------------------------------------
# 🚀 한투 API 직통: 실시간 거래대금 상위 100개 스캔 (토큰 재활용)
# ---------------------------------------------------------
def get_kis_top_trade_value(broker):
    """토큰을 새로 발급하지 않고, 기존 브로커의 연결을 그대로 사용합니다."""
    import requests
    time.sleep(0.5)
    try:
        url = f"{broker.base_url}/uapi/domestic-stock/v1/quotations/trade-value-rank"
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {broker.access_token}",
            "appkey": broker.api_key,
            "appsecret": broker.api_secret,
            "tr_id": "FHPST01670000",
            "custtype": "P"
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "0", "FID_BLNG_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "111111111", "FID_TRGT_EXLS_CLS_CODE": "000000",
            "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "", "FID_VOL_CNT": "", "FID_INPUT_DATE_1": ""
        }
        res = requests.get(url, headers=headers, params=params)
        data = res.json()
        if 'output' in data:
            return [item['mksc_shrn_iscd'] for item in data['output'][:100]]
    except Exception as e:
        # 에러 나도 봇이 멈추지 않게 조용히 넘어갑니다.
        pass 
    return []
def get_real_us_positions(broker):
    """[불필요한 토큰 발급 제거] 모지토 기본 토큰 정제기"""
    import requests
    
    # 💡 [핵심] 계속 새로 발급받는 코드 삭제! 모지토의 기본 토큰에서 "Bearer "만 깔끔하게 지워서 씁니다.
    clean_token = broker.access_token.replace("Bearer ", "").strip()

    is_mock = "vps" in broker.base_url or "vts" in broker.base_url
    tr_id = "VTRP6504R" if is_mock else "CTRP6504R" 
    
    url = f"{broker.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance"
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {clean_token}", # 🚨 깨끗하게 정제된 기존 토큰 재활용!
        "appkey": broker.api_key.strip(),
        "appsecret": broker.api_secret.strip(),
        "tr_id": tr_id,
        "custtype": "P"
    }
    params = {
        "CANO": broker.acc_no.split("-")[0],
        "ACNT_PRDT_CD": broker.acc_no.split("-")[1],
        "WCRC_FRCR_DVSN_CD": "02", # 외화 기준
        "NATN_CD": "840",          
        "TR_MKET_CD": "00",        
        "INQR_DVSN_CD": "00"       
    }
    
    try:
        res = requests.get(url, headers=headers, params=params).json()
        if res.get('rt_cd') != '0':
            print(f"⚠️ [잔고조회 거절됨] {res.get('msg_cd')}: {res.get('msg1')}")
        return res
    except Exception as e:
        print(f"⚠️ 미장 잔고 API 통신 에러: {e}")
        return {}

def execute_us_order_direct(broker, side, ticker, qty, price):
    """[최종 완전판] 한투 미장 직통 주문기 (거래소 자동분류 + 토큰 정제 + Hashkey)"""
    import requests
    
    is_mock = "vps" in broker.base_url or "vts" in broker.base_url
    if side == "buy": 
        tr_id = "VTTT1002U" if is_mock else "TTTT1002U"
    else: 
        tr_id = "VTTT1001U" if is_mock else "TTTT1006U"
        
    # 💡 [핵심 수술 1] 티커별 정확한 거래소(NASD/NYSE) 매핑!
    # XOM, CVX 등 뉴욕(NYSE) 주식을 NASD로 보내면 한투 서버가 뻗으면서 '토큰 에러(EGW00121)'를 뱉습니다.
    nyse_tickers = {
        "XOM", "CVX", "SLB", "AON", "NOC", "CL", "ICE", "GE", "BA", "DIS", 
        "JNJ", "JPM", "KO", "MCD", "MMM", "PFE", "PG", "UNH", "V", "WMT", 
        "CAT", "TRV", "DOW", "IBM", "HON", "RTX", "AMGN", "SYK", "LMT", "T", 
        "SPGI", "BLK", "MDLZ", "TJX", "PGR", "C", "MMC", "CB", "CI", "CVS", 
        "ZTS", "FI", "WM", "CSX", "CME", "MO", "ITW", "SHW", "DUK", "SO", "BDX", "MCO", "EOG",
        "BRK-B", "LLY", "ABBV", "BAC", "TMO", "ACN", "LIN", "ABT", "DHR", "NOW", "COP", "PM", "REGN", "BSX"
    }
    excg_cd = "NYSE" if ticker in nyse_tickers else "NASD"
    
    url = f"{broker.base_url}/uapi/overseas-stock/v1/trading/order"
    
    # 💡 [핵심 수술 2] 가격 소수점 2자리 강제 고정 (이게 안 맞으면 또 튕깁니다)
    price_str = f"{float(price):.2f}"
    
    data = {
        "CANO": broker.acc_no.split("-")[0],
        "ACNT_PRDT_CD": broker.acc_no.split("-")[1],
        "OVRS_EXCG_CD": excg_cd, 
        "PDNO": ticker,
        "ORD_QTY": str(int(qty)),
        "OVRS_ORD_UNPR": price_str,
        "ORD_SVR_DVSN_CD": "0",
        "ORD_DVSN": "00" 
    }
    
    # Hashkey 발급
    hash_url = f"{broker.base_url}/uapi/hashkey"
    hash_headers = {
        "content-type": "application/json",
        "appkey": broker.api_key.strip(),
        "appsecret": broker.api_secret.strip()
    }
    hash_res = requests.post(hash_url, headers=hash_headers, json=data).json()
    hashkey = hash_res.get("HASH", "")
    
    # 💡 [핵심 수술 3] Bearer 중복 방지 (가장 흔한 EGW00121 원인 원천 차단)
    clean_token = broker.access_token.replace("Bearer ", "").strip()
    
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {clean_token}",
        "appkey": broker.api_key.strip(),
        "appsecret": broker.api_secret.strip(),
        "tr_id": tr_id,
        "custtype": "P",
        "hashkey": hashkey
    }
    
    try:
        return requests.post(url, headers=headers, json=data).json()
    except Exception as e:
        return {"rt_cd": "1", "msg1": str(e)}
# =====================================================================
# 🤖 [최종 완성본] 3대 시장 통합 엔진 (비율 매수 + ATS 반영)
# =====================================================================
def run_trading_bot():
    print("" + "="*55)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] V5.0 전문가 엔진 가동 중...")
    """15분마다 실행되는 메인 엔진"""
    global state, kr_cash, us_cash, total_kr_equity, total_us_equity, krw_bal
    print(f"[순찰 시작] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    state = load_state(STATE_PATH)
    # 1. 최신 계좌 정보부터 가져오기 (비교 대상)
    held_kr = get_held_stocks_kr()    # 국장 실제 보유 리스트
    held_us = get_held_stocks_us()    # 미장 실제 보유 리스트
    held_coins = get_held_coins()     # 코인 실제 보유 리스트
    
    
    # --------------------------------------------------------
    # 🛡️ [무한 자가 치유] 순찰 돌 때마다 장부부터 싹 닦습니다!
    # --------------------------------------------------------
    # 이 함수가 실행되면서 실제 계좌랑 안 맞는 유령들을 매번 지워줍니다.
    sync_all_positions(state, held_kr, held_us, held_coins)
    
    weather = get_real_weather(broker_kr, broker_us)
    
    # =======================================================
    # 🎯 30년차 프로의 [Triple-Filter] 하이브리드 사냥터 세팅
    # =======================================================
    try:
        kr_target_file = BASE_DIR / "kr_targets.json"
        with open(kr_target_file, "r", encoding="utf-8") as f:
            scanned_targets = json.load(f)
    except:
        scanned_targets = []

    # 🚀 1. 재료 준비 (시총 상위 200위 + 당일 거래대금 상위 100위)
    market_cap_200 = get_kis_market_cap_rank(broker_kr, limit=200)
    realtime_trade_all = get_kis_top_trade_value(broker_kr)

    # 🚀 2. 전문가의 [티어링] 로직 (수급과 우량도를 동시에!)
    # [Tier 1] 시총 200위 우량주 중 오늘 거래대금 50위 내 '진짜 대장'
    tier_1 = [t for t in realtime_trade_all[:50] if t in market_cap_200]
    # [Tier 2] 시총은 낮지만 오늘 돈이 미친듯이 터지는 '수급주'
    tier_2 = [t for t in realtime_trade_all[:50] if t not in tier_1]
    # [Tier 3] 대표님 조건검색 종목 + 나머지 시총 200위 (안정판)
    tier_3 = list(dict.fromkeys(scanned_targets + market_cap_200))

    # 🚀 3. 최종 병합 (순서 엄수: Tier 1 -> Tier 2 -> Tier 3)
    final_targets = []
    seen = set()
    for t in (tier_1 + tier_2 + tier_3):
        if t not in seen:
            final_targets.append(t)
            seen.add(t)

    print(f"  -> 🌐 [전문가 세팅] 1티어({len(tier_1)}개) 포함 총 {len(final_targets)}개 순차 분석!")

    # =======================================================
    # 🎯 2. 미장 사냥 명단 (나스닥/S&P 대장주 상위 50개 고정)
    # =======================================================
    night_targets = [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B", "LLY", "AVGO",
        "JPM", "V", "XOM", "UNH", "MA", "PG", "JNJ", "HD", "MRK", "COST",
        "ABBV", "CVX", "CRM", "AMD", "NFLX", "KO", "PEP", "BAC", "TMO", "WMT",
        "ACN", "LIN", "MCD", "CSCO", "ABT", "INTC", "QCOM", "INTU", "VZ", "CMCSA",
        "TXN", "DHR", "PFE", "AMAT", "UNP", "IBM", "NOW", "COP", "PM", "BA",
        # 👇 여기서부터 새로 추가된 50개 (우량주 & 고성장주)
        "ISRG", "GE", "HON", "CAT", "RTX", "DIS", "AMGN", "SYK", "LMT", "T",
        "SPGI", "BKNG", "BLK", "MDLZ", "TJX", "ADP", "PGR", "C", "REGN", "VRTX",
        "ADI", "MMC", "CB", "BSX", "CI", "CVS", "ZTS", "FI", "PANW", "SNPS",
        "KLAC", "GILD", "CDNS", "EQIX", "WM", "CSX", "CME", "MO", "ITW", "SHW",
        "DUK", "SO", "BDX", "MCO", "EOG", "SLB", "AON", "NOC", "CL", "ICE"
    ]
    # =======================================================
    # --------------------------------------------------------
    # 🇰🇷 [1] 국장 위성 트레이딩 (V5.0 마스터)
    # --------------------------------------------------------
    if is_market_open("KR"):
        bal = ensure_dict(broker_kr.fetch_balance())
        # --- 안전하게 KR 잔고 데이터 가져오기 (오류 수정) ---
        kr_balance_data = bal.get('output2', []) # 'output2' 키의 값을 가져오거나, 없으면 빈 리스트 반환

        if kr_balance_data: # 잔고 데이터가 존재하면 (리스트가 비어있지 않으면)
            kr_cash = int(kr_balance_data[0].get('prvs_rcdl_excc_amt', 0))
            total_kr_equity = int(kr_balance_data[0].get('tot_evlu_amt', kr_cash))
        else: # 잔고 데이터가 없으면 (빈 리스트이거나 오류 발생 시)
            kr_cash = 0
            total_kr_equity = 0 # 기본값으로 0 설정
        # --- 잔고 데이터 처리 완료 ---
        output1 = safe_get(bal, 'output1', [])
        held_kr = [s['pdno'] for s in output1 if int(s.get('hldg_qty', 0)) > 0]

        if not check_mdd_break("KR", total_kr_equity, state):
            print("  -> 🚨 국장 MDD 브레이크 작동 중. 신규 매수 중단.")
        else:
            print(f"▶️ [🇰🇷 국장] V5.0 마스터 추세추종 가동 중... (날씨: {weather['KR']})")
            
            # 🛑 1. 매도 (샹들리에 트레일링 스탑)
            for stock in output1:
                t = stock['pdno']
                if t in CORE_ASSETS or t not in state.get("positions", {}): 
                    continue
                
                ohlcv = get_kis_ohlcv(broker_kr, t)
                if not ohlcv: continue
                curr_p = float(ohlcv[-1]['c']) 
                
                pos_info = state.get("positions", {}).get(t, {})
                buy_p = pos_info.get('buy_p', curr_p) # 내가 산 가격
                
                # 💡 [핵심 수술] 
                # 시장의 고점이 아니라, '내가 산 가격'과 '현재가' 중 더 높은 놈을 최고가로 봅니다.
                # 이렇게 하면 내가 사기 전의 장중 고점(10,500원)은 완벽하게 무시됩니다!
                pos_info['max_p'] = max(pos_info.get('max_p', buy_p), curr_p)
                state.setdefault("positions", {})[t] = pos_info
                
                # 🚨 [안전 장치] 산 가격보다 주가가 1% 이상 오르기 전까지는 샹들리에 매도를 작동시키지 않습니다.
                # (사자마자 0.3% 흔들림에 털리는 걸 방지하는 '숨 고르기' 구간입니다.)
                if curr_p < buy_p * 1.01:
                    continue 

                is_exit, reason = check_pro_exit(curr_p, pos_info, ohlcv)
                
                if is_exit:
                    resp = broker_kr.create_market_sell_order(t, int(stock['hldg_qty']))
                    if resp.get('rt_cd') == '0':
                        # ==========================================================
                        # 💡 [승률 기록원] 국장 익절/손절 계산기
                        profit_rate = ((curr_p - buy_p) / buy_p) * 100
                        stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})
                        if profit_rate > 0:
                            stats["wins"] += 1
                        else:
                            stats["losses"] += 1
                        # ==========================================================
                        
                        msg = f"🚨 [국장 추세종료 매도] {t}\n사유: {reason}\n최종 수익률: {profit_rate:.2f}%"
                        print(f"\n{msg}")
                        send_telegram(msg)
                        
                        del state["positions"][t]
                        set_cooldown(state, t)
                        save_state(STATE_PATH, state)

            # 🚀 국장 매수
            if is_market_open("KR"):
                total_kr = len(final_targets)
                print(f"  -> 🇰🇷 국장 사냥감 {total_kr}개 정밀 분석 시작!")
                
                for idx, t in enumerate(final_targets, 1): 
                    if t in CORE_ASSETS or t in held_kr or in_cooldown(state, t): 
                        continue
                    # 1. 200일선 전략은 과거 데이터가 빵빵한 야후 파이낸스로!
                    ohlcv_200 = get_ohlcv_yfinance(t)
                    is_buy, sl_p, s_name = calculate_pro_signals(ohlcv_200, weather['KR'], t, idx, len(final_targets))
                    
                    if is_buy:
                        # 2. 5% 갭상승 컷오프는 딜레이 없는 한투 실시간 API로!
                        realtime_data = get_ohlcv_realtime(broker_kr, t)
                        try:
                            prev_close = realtime_data[-2]['c']
                            today_open = realtime_data[-1]['o']
                            gap_up_rate = ((today_open - prev_close) / prev_close) * 100
                        except Exception as e:
                            print(f"   ⚠️ [{t}] 실시간 갭상승 계산 에러({e}). 일단 통과.")
                            gap_up_rate = 0 
                        
                        # 🛡️ 5% 이상 갭상승 시 단호박 컷오프
                        if gap_up_rate >= 5.0:
                            print(f"   🛡️ [{t}] 갭상승 {gap_up_rate:.2f}% (5% 이상). 불꽃놀이 꼭대기 물림 방지! 매수 패스.")
                            continue # 아래 매수 로직 안 타고 다음 종목으로!
                        
                        # 💡 [30년차의 뼈 때리는 비중 조절] - 시장 상황에 따라 공격적으로!
                        if weather['KR'] == "☀️ BULL": # 불장엔 공격적으로!
                            if t in tier_1: ratio, t_name = 0.60, "1티어(우량대장) - 불장" # 우량주는 60% 풀베팅
                            elif t in tier_2: ratio, t_name = 0.40, "2티어(수급급등) - 불장" # 수급주는 40% 공격적
                            else: ratio, t_name = 0.30, "3티어(기타/패턴) - 불장" # 나머지는 30% 방어적
                        elif weather['KR'] == "☁️ SIDEWAYS": # 횡보장엔 조금 더 보수적으로
                            if t in tier_1: ratio, t_name = 0.40, "1티어(우량대장) - 횡보" # 40%
                            elif t in tier_2: ratio, t_name = 0.30, "2티어(수급급등) - 횡보" # 30%
                            else: ratio, t_name = 0.20, "3티어(기타/패턴) - 횡보" # 20%
                        else: # 그 외 (하락장, 혹시 모를 상황 대비)
                            ratio, t_name = 0.10, "기타 - 방어" # 기존 10% 유지

                        target_budget = total_kr_equity * ratio
                        
                        if not can_open_new(t, state, max_positions=8):
                            print(f"   ⚠️ [{t}] 타점이나 8종목 초과로 관망 (현재 {len(held_kr)}개)")
                            continue

                        if kr_cash >= target_budget and target_budget > 50000:
                            curr_p = float(ohlcv[-1]['c'])
                            qty = int(target_budget / curr_p)
                            if qty > 0:
                                resp = broker_kr.create_market_buy_order(t, qty)
                                if resp.get('rt_cd') == '0':
                                    send_telegram(f"🎯 [{t_name} 매수] {t}비중: {ratio*100:.0f}% / 손절가: {int(sl_p):,}원")
                                    kr_cash -= (qty * curr_p)
                                    state.setdefault("positions", {})[t] = {'buy_p': curr_p, 'sl_p': sl_p, 'max_p': curr_p, 'tier': t_name}
                                    save_state(STATE_PATH, state)
                                else:
                                    print(f"   ❌ [{t}] 주문 실패! 한투 응답: {resp.get('msg1')}")
                            else:
                                print(f"   ❌ [{t}] 1주도 살 수 없는 비싼 주식입니다.")
                        else:
                            print(f"   ❌ [{t}] 계좌 현금 부족으로 패스! (다른 놈들이 돈을 다 썼습니다)")
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 💤 국장은 현재 휴장 또는 장 마감 상태입니다.")


    # --------------------------------------------------------
    # 🇺🇸 [2] 미장 위성 트레이딩 (V5.0 마스터)
    # --------------------------------------------------------
    if is_market_open("US"):
        us_cash = get_us_cash_real(broker_us)
        us_bal = get_real_us_positions(broker_us) # 💡 모지토 버리고 직통 스캐너 출동!
        
        out2 = safe_get(us_bal, 'output2', {})
        us_stock_value = float(out2.get('ovrs_stck_evlu_amt', 0)) # 해외주식 평가금액
        total_us_equity = us_cash + us_stock_value # 총자산 완벽 계산
        
        us_output1 = safe_get(us_bal, 'output1', [])
        # 💡 미장의 진짜 수량 단어인 'ovrs_cblc_qty' 사용!
        held_us = [s['ovrs_pdno'] for s in us_output1 if float(s.get('ovrs_cblc_qty', 0)) > 0]
        # 🛑 MDD 킬 스위치 검사 먼저!
        if not check_mdd_break("US", total_us_equity, state):
            print("  -> 🚨 미장 계좌 MDD 브레이크 작동 중 (-5% 하락). 모든 매수 올스톱!")
        else:
            print(f"▶️ [🇺🇸 미장] V5.0 마스터 추세추종 가동 중... (날씨: {weather['US']})")

            # 🛑 1. 매도 (미장 샹들리에 트레일링 스탑)
            for stock in us_output1:
                t = stock['ovrs_pdno']
                if t in CORE_ASSETS or t not in state.get("positions", {}): 
                    continue 
                
                ohlcv = get_kis_ohlcv(broker_us, t)
                if not ohlcv: continue
                curr_p = float(ohlcv[-1]['c'])
                
                pos_info = state.get("positions", {}).get(t, {})
                buy_p = pos_info.get('buy_p', curr_p) # 내가 산 가격
                
                # 💡 [핵심] 시장의 장중 고점이 아니라 '내 진입가'와 '현재가' 중 높은 것을 최고가로 갱신
                pos_info['max_p'] = max(pos_info.get('max_p', buy_p), curr_p)
                state.setdefault("positions", {})[t] = pos_info
                
                # 🚨 [1% 보호막] 산 가격보다 1% 이상 오르기 전에는 노이즈에 팔지 않음
                if curr_p < buy_p * 1.01:
                    continue 
                
                is_exit, reason = check_pro_exit(curr_p, pos_info, ohlcv)
                
                if is_exit:
                    sell_price = round(curr_p * 0.98, 2)
                    resp = execute_us_order_direct(broker_us, "sell", t, float(stock['hldg_qty']), sell_price)
                    if resp.get('rt_cd') == '0':
                        # ==========================================================
                        # 💡 [핵심 추가] 승률 기록원 출동! (익절인지 손절인지 계산)
                        buy_p = pos_info.get('buy_p', curr_p) # 내가 샀던 가격
                        profit_rate = ((sell_price - buy_p) / buy_p) * 100 # 수익률 계산
                        
                        stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})
                        if profit_rate > 0:
                            stats["wins"] += 1   # 익절이면 1승 추가!
                        else:
                            stats["losses"] += 1 # 손절이면 1패 추가!
                        # ==========================================================
                        
                        # 텔레그램 메시지에도 수익률을 예쁘게 찍어줍니다!
                        msg = f"🚨 [미장 추세종료 매도] {t}\n사유: {reason}\n최종 수익률: {profit_rate:.2f}%"
                        print(f"\n{msg}")
                        send_telegram(msg)
                        
                        del state["positions"][t] # 장부에서 삭제
                        set_cooldown(state, t)
                        save_state(STATE_PATH, state)

            # 🚀 미장 매수 (하락장만 아니면 주도주 사냥)
            if weather['US'] != "🌧️ BEAR" and can_open_new(t, state, max_positions=5):
                total_us = len(night_targets)
                print(f"  -> 🇺🇸 미장 대장주 {total_us}개 정밀 분석 시작!")
                
                # 💡 [예산 고정] 1종목당 사용할 달러 예산을 미리 계산 (시장 상황에 따라 공격적으로!)
                if weather['US'] == "☀️ BULL":
                    target_budget = us_cash * 0.50 # 불장에는 50%
                elif weather['US'] == "☁️ SIDEWAYS":
                    target_budget = us_cash * 0.40 # 횡보장에는 40%
                else: # BEAR
                    target_budget = us_cash * 0.30 # 하락장은 기존 30% 유지

                for idx, t in enumerate(night_targets, 1):
                    if t in CORE_ASSETS or t in held_us or in_cooldown(state, t): 
                        continue
                    
                    ohlcv = get_kis_ohlcv(broker_us, t)
                    is_buy, sl_p, s_name = calculate_pro_signals(ohlcv, weather['US'], t, idx, total_us)
                    
                    if is_buy:
                        print(f"   💸 [{t}] 예산 확인: 남은 예수금 ${us_cash:.2f} / 필요 배정액 ${target_budget:.2f}")
                        
                        # 🚨 지갑에 남은 돈이 배정액보다 많고, 최소 주문 금액($500) 이상일 때만 진입
                        if us_cash >= target_budget and target_budget > 200:
                            curr_p = float(ohlcv[-1]['c'])
                            qty = int(target_budget / curr_p) 
                            
                            if qty > 0:
                                buy_price = round(curr_p * 1.01, 2)
                                resp = execute_us_order_direct(broker_us, "buy", t, qty, buy_price)
                                print(f"   📡 [{t}] 미장 한투 API 주문 응답: {resp}") 
                                
                                if resp.get('rt_cd') == '0':
                                    msg = f"🎯 [미장 V5.0 기관매수] {t} ({qty}주 체결!)전략: {s_name}초기 손절가: ${sl_p:.2f}"
                                    send_telegram(msg)
                                    
                                    # ✅ [치매 치료] 돈을 썼으니 내 지갑(us_cash)에서 실제 매수액만큼 뺍니다!
                                    us_cash -= (qty * curr_p)
                                    
                                    state.setdefault("positions", {})[t] = {
                                        'buy_p': curr_p,     # 진입가
                                        'sl_p': sl_p,        # 하드스탑
                                        'max_p': curr_p      # 최고가 (진입가로 시작)
                                    }
                                    save_state(STATE_PATH, state)
                                else:
                                    print(f"   ❌ [{t}] 주문 실패! 한투 응답: {resp.get('msg1')}")
                            else:
                                print(f"   ❌ [{t}] 배정액으로 1주도 살 수 없는 주식입니다.")
                        else:
                            print(f"   ❌ [{t}] 미장 현금 부족 패스 (지갑이 비었거나 최소 금액 미달)")
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 💤 미장은 현재 휴장 또는 장 마감 상태입니다.")
        
    # --------------------------------------------------------
    # 🪙 [3] 코인 위성 트레이딩 (V5.0 마스터)
    # --------------------------------------------------------
    if is_market_open("COIN"):
        coin_weather = weather.get('COIN', '☁️ SIDEWAYS')
        
        
        balances = upbit.get_balances()
        krw_bal = float(next((b['balance'] for b in balances if b['currency'] == 'KRW'), 0))
        # 현재 봇이 쥐고 있는 코인 목록
        held_coins = [f"KRW-{b['currency']}" for b in balances if b['currency'] not in ['KRW', 'VTHO'] and float(b['avg_buy_price']) > 0]
        # 🚨 [추가됨] 코인 총 평가금액 (현금 + 들고 있는 코인 현재가치) 계산
        total_coin_equity = krw_bal
        for b in balances:
            if b['currency'] not in ['KRW', 'VTHO']:
                curr_p = pyupbit.get_current_price(f"KRW-{b['currency']}")
                if curr_p:
                    total_coin_equity += (float(b['balance']) * curr_p)
        
        # 🛑 MDD 킬 스위치 검사 먼저!
        if not check_mdd_break("COIN", total_coin_equity, state):
            print("  -> 🚨 코인 계좌 MDD 브레이크 작동 중 (-5% 하락). 모든 매수 올스톱!")
        else:
            print(f"▶️ [🪙 코인] V5.0 마스터 추세추종 가동 중... (날씨: {coin_weather})")
            # 🛑 1. 매도 (코인 샹들리에 트레일링 스탑)
            for b in balances:
                if b['currency'] in ['KRW', 'VTHO']: continue
                t = f"KRW-{b['currency']}"
                if t not in state.get("positions", {}): 
                    continue
                qty = float(b['balance'])
                if qty <= 0.0001: continue

                curr_p = pyupbit.get_current_price(t)
                df_upbit = pyupbit.get_ohlcv(t, interval="day", count=250)
                if df_upbit is None or len(df_upbit) < 20: continue
                ohlcv = [{'o': row['open'], 'h': row['high'], 'l': row['low'], 'c': row['close'], 'v': row['volume']} for _, row in df_upbit.iterrows()]
                
                pos_info = state.get("positions", {}).get(t, {})
                buy_p = pos_info.get('buy_p', curr_p) # 내가 산 가격
                
                # 💡 [핵심] 내가 산 시점부터의 최고가만 추적함
                pos_info['max_p'] = max(pos_info.get('max_p', buy_p), curr_p)
                state.setdefault("positions", {})[t] = pos_info
                
                # 🚨 [1% 보호막] 코인 특유의 잔파동에 털리지 않게 1% 상승 전까지는 매도 감시 유예
                if curr_p < buy_p * 1.01:
                    continue 
                
                is_exit, reason = check_pro_exit(curr_p, pos_info, ohlcv)
                
                if is_exit:
                    resp = upbit.sell_market_order(t, qty)
                    if resp:
                        # ==========================================================
                        # 💡 [승률 기록원] 코인 익절/손절 계산기
                        profit_rate = ((curr_p - buy_p) / buy_p) * 100
                        stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})
                        if profit_rate > 0:
                            stats["wins"] += 1
                        else:
                            stats["losses"] += 1
                        # ==========================================================
                        
                        msg = f"🚨 [코인 추세종료 매도] {t}\n사유: {reason}\n최종 수익률: {profit_rate:.2f}%"
                        print(f"\n{msg}")
                        send_telegram(msg)
                        
                        del state["positions"][t]
                        set_cooldown(state, t)
                        save_state(STATE_PATH, state)

            # 🚀 2. 코인 매수 (하락장만 아니면 실시간 깡패 50개 스캔)
            if coin_weather != "🌧️ BEAR": 
                try:
                    # 🚀 [코인 20개 한정] 거래대금 상위 20개만 정밀하게 긁어옵니다.
                    markets = [m['market'] for m in requests.get("https://api.upbit.com/v1/market/all").json() if m['market'].startswith("KRW-")]
                    tickers_data = requests.get("https://api.upbit.com/v1/ticker?markets=" + ",".join(markets)).json()
                    scan_targets = [x['market'] for x in sorted(tickers_data, key=lambda x: x['acc_trade_price_24h'], reverse=True)[:20]] # 💡 20개 제한
                except:
                    scan_targets = ["KRW-BTC", "KRW-ETH"]

                print(f"  -> 🪙 코인 실시간 수급 상위 {len(scan_targets)}개 정밀 분석 시작! (시황: {coin_weather})")

                for idx, t in enumerate(scan_targets, 1):
                    if t in held_coins or in_cooldown(state, t): continue
                    
                    df_upbit = pyupbit.get_ohlcv(t, interval="day", count=250)
                    if df_upbit is None or len(df_upbit) < 20: continue
                    
                    ohlcv = [{'o': row['open'], 'h': row['high'], 'l': row['low'], 'c': row['close'], 'v': row['volume']} for _, row in df_upbit.iterrows()]
                    
                    # 💡 [핵심] 개별 분석 함수에 '전체 날씨'를 강제로 주입합니다.
                    is_buy, sl_p, s_name = calculate_pro_signals(ohlcv, coin_weather, t, idx, len(scan_targets))
                    
                    if is_buy:
                        # 🚨 [대표님 지적 사항 완벽 반영] 코인도 가드의 통제를 받게 합니다! (예: 3개)
                        if not can_open_new(t, state, max_positions=3):
                            print(f"   ⚠️ [{t}] 코인 슬롯(3종목) 꽉 차서 관망")
                            continue

                        # 💡 [예산 고정] 1종목당 사용할 원화 예산을 미리 계산 (시장 상황에 따라 공격적으로!)
                        if coin_weather == "☀️ BULL":
                            budget = krw_bal * 0.50 # 불장에는 50%
                        elif coin_weather == "☁️ SIDEWAYS":
                            budget = krw_bal * 0.40 # 횡보장에는 40%
                        else: # BEAR
                            budget = krw_bal * 0.30 # 하락장은 기존 30% 유지
                            
                        print(f"   💸 [{t}] 코인 예산 확인: 배정액 {int(budget):,}원")
                        if budget > 5500: 
                            # 🚨 코인은 업비트 규칙에 따라 budget(금액)을 그대로 밀어 넣는 게 맞습니다!
                            resp = upbit.buy_market_order(t, budget)
                            print(f"   📡 [{t}] 업비트 API 주문 응답: {resp}") 
                            
                            if resp: 
                                msg = f"🎯 [코인 V5.0 기관매수] {t}\n전략: {s_name}\n초기 손절가(하드스탑): {sl_p:,.0f}원"
                                send_telegram(msg)
                                
                                current_p = pyupbit.get_current_price(t)
                                state.setdefault("positions", {})[t] = {
                                    'buy_p': current_p,  
                                    'sl_p': sl_p,        
                                    'max_p': current_p   
                                }
                                save_state(STATE_PATH, state)
                                held_coins.append(t)
                                krw_bal -= budget
                                # 🗑️ [삭제] 기존에 있던 'if len(held_coins) >= 3: break' 원시 코드는 지워버립니다!
                        else:
                            print(f"   ❌ [{t}] 업비트 현금 부족 패스 (배정액 {int(budget):,}원 < 5500원)")
    
    print("="*55 + "")

# =====================================================================
# 📡 1. GUI 로그창으로 바로 쏘는 스캐너 실행
# =====================================================================
def run_screener_for_gui():
    """파일로 숨기지 않고, 평소처럼 print()를 써서 GUI 화면에 로그를 쫙 뿌립니다."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📡 백그라운드 스캐너 자동 가동")
    try:
        screener.run_night_screener()
    except Exception as e:
        print(f"⚠️ 스캐너 실행 중 에러 발생 (인터넷/API 문제): {e}")

# =====================================================================
# 🛡️ 2. 인터넷 끊김 방어 (10초 휴식 후 재시작)
# =====================================================================
def background_schedule_loop():
    """인터넷이 끊기면 봇이 죽는 대신 10초만 쉬었다가 다시 일합니다."""
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            # 👉 여기서 인터넷 끊김 에러를 흡수하고 뻗지 않게 막아줍니다!
            print(f"⚠️ 네트워크 일시 중단 감지. 10초 대기 후 재시도합니다... ({e})")
            time.sleep(10)
        
        time.sleep(1)

# ---------------------------------------------------------------------
# 🚀 시스템 최종 시동
# ---------------------------------------------------------------------
# 1. 시작하자마자 스캐너 1회 즉시 실행 (GUI 창에 로그 올라오는지 확인!)
run_screener_for_gui()

# 2. 매일 장 열리기 15분 전(08:45)에 스캐너 실행 예약
schedule.every().day.at("08:45").do(run_screener_for_gui)

# 3. 백그라운드 스케줄러 투입 (인터넷 방어막 장착)
scanner_thread = threading.Thread(target=background_schedule_loop, daemon=True)
scanner_thread.start()
