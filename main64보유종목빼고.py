import time, json, schedule, pyupbit, requests, traceback, atexit, threading, sys, os
import mojito
import pytz
from pathlib import Path
from datetime import datetime, timedelta
import yfinance as yf
import pandas as pd
import pandas_market_calendars as mcal

from risk.guard import load_state, save_state, in_cooldown, set_cooldown, can_open_new, check_mdd_break
from strategy.rules import get_ohlcv_yfinance, get_ohlcv_realtime, calculate_pro_signals, check_pro_exit
import screener

def calculate_atr(ohlcv):
    """주어진 OHLCV 데이터로 ATR(14)을 계산합니다."""
    if not ohlcv or len(ohlcv) < 15:
        return 0
    df = pd.DataFrame(ohlcv)
    df['tr0'] = abs(df['h'] - df['l'])
    df['tr1'] = abs(df['h'] - df['c'].shift())
    df['tr2'] = abs(df['l'] - df['c'].shift())
    df['tr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
    return df['tr'].rolling(14).mean().iloc[-1]

KIS_TOKEN = None

# =====================================================================
# 0. 기본 설정
# =====================================================================
BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "bot_state.json"
TRADE_HISTORY_PATH = BASE_DIR / "trade_history.json"
KIS_TOKEN_PATH = BASE_DIR / "kis_token.json"

with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

# 브로커 객체를 전역 변수로 선언
broker_kr = None
broker_us = None
upbit = None

# 🛡️ [대장주 보호막] 봇이 절대 건드리지 않는 종목
CORE_ASSETS = ["005930", "000660", "QQQ", "NVDA", "TSLA", "AAPL", "MSFT"]

# 종목 명칭 딕셔너리
kr_name_dict = {"005930": "삼성전자", "000660": "SK하이닉스", "035420": "NAVER", "035720": "카카오", "005380": "현대차", "069500": "KODEX 200"}
us_name_dict = {"AAPL": "애플", "MSFT": "마이크로소프트", "NVDA": "엔비디아", "TSLA": "테슬라", "AMZN": "아마존"}

# =====================================================================
# 1. 텔레그램 및 시스템 안정성
# =====================================================================
def send_telegram(message):
    """텔레그램 메시지 전송"""
    url = f"https://api.telegram.org/bot{config['telegram_token']}/sendMessage"
    params = {"chat_id": config['telegram_chat_id'], "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, params=params, timeout=10)
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 실패: {e}")

def shutdown_handler():
    """프로그램 비정상 종료 시 에러 보고"""
    err = traceback.format_exc()
    if "SystemExit" not in err and "KeyboardInterrupt" not in err and "NoneType: None" not in err and err.strip() != 'None':
        send_telegram(f"🚨 [긴급] `main64.py` 봇 에러 발생:\n```{repr(err[-250:])}```")

atexit.register(shutdown_handler)

# =====================================================================
# 2. 토큰 관리 및 API 안정성
# =====================================================================
def _create_brokers():
    """Mojito 브로커 객체를 (재)생성합니다."""
    global broker_kr, broker_us, upbit
    try:
        broker_kr = mojito.KoreaInvestment(
            api_key=config["kis_key"], api_secret=config["kis_secret"],
            acc_no=config["kis_account"], exchange='서울'
        )
        broker_us = mojito.KoreaInvestment(
            api_key=config["kis_key"], api_secret=config["kis_secret"],
            acc_no=config["kis_account"], exchange='나스닥'
        )
        upbit = pyupbit.Upbit(config["upbit_access"], config["upbit_secret"])
        
        # KIS 토큰 발급 (mojito는 자동 발급 안 함)
        token_data = issue_new_kis_token()
        if token_data and 'access_token' in token_data:
            broker_kr.access_token = token_data['access_token']
            broker_us.access_token = token_data['access_token']
        else:
            print("⚠️ 토큰 발급 실패 - 기존 토큰 사용 시도")
            token_data = load_kis_token()
            if token_data and 'access_token' in token_data:
                broker_kr.access_token = token_data['access_token']
                broker_us.access_token = token_data['access_token']
    except Exception as e:
        print(f"🚨 브로커 객체 생성 실패: {e}")
        send_telegram(f"🚨 [긴급] 브로커 객체 생성에 실패했습니다. 키/계좌번호 설정을 확인하세요.\n{e}")
        sys.exit(1)

def load_kis_token():
    """파일에서 토큰 정보 로드"""
    if KIS_TOKEN_PATH.exists():
        with open(KIS_TOKEN_PATH, "r") as f:
            try: return json.load(f)
            except json.JSONDecodeError: return None
    return None

def save_kis_token(token_data):
    """토큰 정보를 파일에 저장"""
    with open(KIS_TOKEN_PATH, "w") as f:
        json.dump(token_data, f)

def issue_new_kis_token():
    """새로운 토큰을 발급받아 파일에 저장합니다."""
    print("  -> ⏳ 새로운 KIS 토큰 발급을 시도합니다.")
    try:
        # 🔧 직접 API를 호출하여 토큰을 발급받습니다
        auth_url = "https://openapi.koreainvestment.com:9443/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": config["kis_key"],
            "appsecret": config["kis_secret"]
        }
        res = requests.post(auth_url, json=body, verify=False)
        token_data = res.json()
        
        if 'access_token' in token_data:
            token_data['timestamp'] = datetime.now().timestamp()
            save_kis_token(token_data)
            print("  -> ✅ 새 토큰 발급 및 저장 성공!")
            return token_data
        else:
            print(f"🚨 토큰 발급 응답 오류: {token_data}")
            return None
    except Exception as e:
        print(f"🚨 [긴급] 토큰 발급 실패: {e}")
        send_telegram(f"🚨 [긴급] KIS 토큰 발급에 실패했습니다: {e}")
        return None

def refresh_brokers_if_needed(force=False):
    """토큰을 확인하고 필요 시 재발급합니다."""
    global broker_kr, broker_us
    
    if force:
        print("  -> ⚠️ API 오류 감지! 브로커 재생성 (토큰 재발급)")
        _create_brokers()
        print("  -> ✅ 브로커 재생성 완료")
        return
    
    # 브로커가 없으면 생성
    if broker_kr is None or broker_us is None:
        _create_brokers()
        print("  -> ✅ 브로커 초기화 완료")
        return
    
    # 토큰 만료 체크 (11시간 50분마다 재발급)
    token_data = load_kis_token()
    if token_data and 'timestamp' in token_data:
        issue_time = datetime.fromtimestamp(token_data['timestamp'])
        if datetime.now() >= issue_time + timedelta(hours=11, minutes=50):
            print("  -> ⏳ 토큰 만료 임박 - 재발급 시작")
            new_token = issue_new_kis_token()
            if new_token and 'access_token' in new_token:
                broker_kr.access_token = new_token['access_token']
                broker_us.access_token = new_token['access_token']
                print("  -> ✅ 토큰 재발급 완료")
            else:
                print("  -> ⚠️ 토큰 재발급 실패")
        else:
            print("  -> ✅ 토큰 유효")
    else:
        print("  -> ⚠️ 토큰 파일 없음 - 브로커 재생성")
        _create_brokers()

# =====================================================================
# 3. 유틸리티
# =====================================================================
def _to_float(v, default=0.0) -> float:
    try:
        if v is None: return float(default)
        if isinstance(v, str): v = v.replace(",", "").strip()
        return float(v)
    except (ValueError, TypeError):
        return float(default)

def _safe_num(value, default=0.0):
    """안전한 숫자 변환 (튜플, 문자열, None 모두 처리)"""
    try:
        if isinstance(value, tuple) and value:
            value = value[0]
        return _to_float(value, default)
    except Exception:
        return float(default)

def _split_account_no(acc_no: str):
    try:
        raw = (acc_no or "").strip()
        if "-" in raw:
            cano, prdt = raw.split("-", 1)
            return cano.strip(), prdt.strip()
        return raw[:8].strip(), raw[8:].strip()
    except Exception:
        return "", ""

def safe_get(data, key, default=None):
    """데이터가 딕셔너리일 때만 .get()을 호출합니다."""
    if isinstance(data, dict):
        return data.get(key, default)
    return default

def ensure_dict(data):
    """API 응답이 list로 잘못 올 경우 빈 dict로 안전하게 교체합니다."""
    return data if isinstance(data, dict) else {}

def format_symbol_name(name, ticker):
    """종목 표시용 문자열 (이름이 코드와 같으면 코드만 표시)"""
    return f"{name}({ticker})" if name and str(name) != str(ticker) else str(ticker)

def is_market_open(market="KR"):
    """한국, 미국, 코인 시장의 개장 여부를 확인"""
    if market == "COIN": return True
    
    krx_cal = mcal.get_calendar('XKRX')
    nyse_cal = mcal.get_calendar('NYSE')
    
    now_utc = pd.Timestamp.now(tz='UTC')
    
    if market == "KR":
        cal = krx_cal
        now_local = now_utc.astimezone(pytz.timezone('Asia/Seoul'))
    elif market == "US":
        cal = nyse_cal
        now_local = now_utc.astimezone(pytz.timezone('US/Eastern'))
    else:
        return False
        
    if market == "KR" and now_local.weekday() >= 5: return False

    today_str = now_local.strftime('%Y-%m-%d')
    schedule_cal = cal.schedule(start_date=today_str, end_date=today_str)
    if schedule_cal.empty: return False
        
    market_open = schedule_cal.iloc[0]['market_open']
    market_close = schedule_cal.iloc[0]['market_close']
    
    return market_open <= now_utc <= market_close

def record_trade(trade_info):
    """매매 내역을 JSON 파일에 기록"""
    history = []
    if TRADE_HISTORY_PATH.exists():
        with open(TRADE_HISTORY_PATH, 'r', encoding='utf-8') as f:
            try: history = json.load(f)
            except json.JSONDecodeError: pass
    
    history.append(trade_info)
    
    with open(TRADE_HISTORY_PATH, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

def _record_trade_event(market, ticker, side, qty, price=None, profit_rate=None, reason=""):
    """매매 이벤트를 누적 저장용 JSON에 append"""
    record_trade({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market": market,
        "ticker": ticker,
        "side": side,
        "qty": qty,
        "price": price,
        "profit_rate": profit_rate,
        "reason": reason,
    })

def get_us_cash_real(broker):
    """[직통] 미장 예수금 상세 조회 (토큰 재활용)"""
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
# 4. 조회 함수들 (올려주신 파일 기반)
# =====================================================================
# 4. 조회 함수 - KIS API 직통 스캐너
# =====================================================================
def get_kis_ohlcv(broker, code, timeframe='D', count=60):
    """KIS API로 OHLCV 가져오기"""
    try:
        import requests
        is_mock = "vps" in broker.base_url or "vts" in broker.base_url
        tr_id = "FHKST01010100" if not is_mock else "VHKST01010100"
        
        url = f"{broker.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {broker.access_token}",
            "appkey": broker.api_key,
            "appsecret": broker.api_secret,
            "tr_id": tr_id,
            "custtype": "P"
        }
        params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code, "fid_input_date_1": "", "fid_input_date_2": "", "fid_period_div_code": "D", "fid_org_adj_prc": "1", "fid_adj_prc_div_code": "00100"}
        
        res = requests.get(url, headers=headers, params=params)
        data = res.json()
        if 'output2' in data:
            return [{'o': float(x['open']), 'h': float(x['high']), 'l': float(x['low']), 'c': float(x['close']), 'v': float(x['volume'])} 
                    for x in reversed(data['output2'][:count])]
        return []
    except:
        return []

def get_kis_top_trade_value(broker, limit=100):
    """거래대금 상위 종목 100개"""
    try:
        import requests
        is_mock = "vps" in broker.base_url or "vts" in broker.base_url
        tr_id = "FHPST01670000" if not is_mock else "VHPST01670000"
        
        url = f"{broker.base_url}/uapi/domestic-stock/v1/quotations/trade-value-rank"
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {broker.access_token}",
            "appkey": broker.api_key,
            "appsecret": broker.api_secret,
            "tr_id": tr_id,
            "custtype": "P"
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "0"
        }
        res = requests.get(url, headers=headers, params=params)
        data = res.json()
        if 'output' in data:
            return [item['mksc_shrn_iscd'] for item in data['output'][:limit]]
        return []
    except:
        return []

def get_kis_market_cap_rank(broker, limit=100):
    """시가총액 상위 종목"""
    try:
        import requests
        is_mock = "vps" in broker.base_url or "vts" in broker.base_url
        tr_id = "FHPST01740000" if not is_mock else "VHPST01740000"
        
        url = f"{broker.base_url}/uapi/domestic-stock/v1/quotations/market-cap-rank"
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {broker.access_token}",
            "appkey": broker.api_key,
            "appsecret": broker.api_secret,
            "tr_id": tr_id,
            "custtype": "P"
        }
        res = requests.get(url, headers=headers)
        data = res.json()
        if 'output' in data:
            return [item['mksc_shrn_iscd'] for item in data['output'][:limit]]
        return []
    except:
        return []

def get_real_us_positions(broker):
    """미장 보유 종목 직통 조회"""
    try:
        import requests
        clean_token = broker.access_token.replace("Bearer ", "").strip()
        is_mock = "vps" in broker.base_url or "vts" in broker.base_url
        tr_id = "VTRP6504R" if is_mock else "CTRP6504R"
        
        url = f"{broker.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance"
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {clean_token}",
            "appkey": broker.api_key.strip(),
            "appsecret": broker.api_secret.strip(),
            "tr_id": tr_id,
            "custtype": "P"
        }
        params = {
            "CANO": broker.acc_no.split("-")[0],
            "ACNT_PRDT_CD": broker.acc_no.split("-")[1],
            "WCRC_FRCR_DVSN_CD": "02",
            "NATN_CD": "840",
            "TR_MKET_CD": "00",
            "INQR_DVSN_CD": "00"
        }
        res = requests.get(url, headers=headers, params=params)
        return res.json()
    except:
        return {}

def execute_us_order_direct(broker, side, ticker, qty, price):
    """미장 직통 주문"""
    try:
        import requests
        is_mock = "vps" in broker.base_url or "vts" in broker.base_url
        if side == "buy":
            tr_id = "VTTT1002U" if is_mock else "TTTT1002U"
        else:
            tr_id = "VTTT1001U" if is_mock else "TTTT1006U"
        
        nyse_tickers = {"XOM", "CVX", "SLB", "AON", "NOC", "BA", "DIS", "JNJ", "JPM", "KO", "MCD", "MMM", "PFE", "PG", "UNH", "V", "WMT"}
        excg_cd = "NYSE" if ticker in nyse_tickers else "NASD"
        
        url = f"{broker.base_url}/uapi/overseas-stock/v1/trading/order"
        data = {
            "CANO": broker.acc_no.split("-")[0],
            "ACNT_PRDT_CD": broker.acc_no.split("-")[1],
            "OVRS_EXCG_CD": excg_cd,
            "PDNO": ticker,
            "ORD_QTY": str(int(qty)),
            "OVRS_ORD_UNPR": f"{float(price):.2f}",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00"
        }
        
        hash_url = f"{broker.base_url}/uapi/hashkey"
        hash_headers = {
            "content-type": "application/json",
            "appkey": broker.api_key.strip(),
            "appsecret": broker.api_secret.strip()
        }
        hash_res = requests.post(hash_url, headers=hash_headers, json=data).json()
        hashkey = hash_res.get("HASH", "")
        
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
        
        return requests.post(url, headers=headers, json=data).json()
    except:
        return {"rt_cd": "1", "msg1": "주문 실패"}

# =====================================================================
def get_held_stocks_kr():
    """🇰🇷 국장 실제 보유 종목 코드 리스트 가져오기"""
    try:
        bal = get_balance_with_retry()
        if not bal:
            return []
        if 'output1' not in bal:
            return []
        # hldg_qty 우선, 없으면 ccld_qty_smtl1 사용
        return [s['pdno'] for s in bal['output1'] if int(s.get('hldg_qty', s.get('ccld_qty_smtl1', 0))) > 0]
    except Exception as e:
        print(f"⚠️ 국장 잔고 조회 실패: {e}")
        return []

def get_held_stocks_us():
    """🇺🇸 미장 실제 보유 종목 티커 리스트 가져오기"""
    try:
        bal = get_us_positions_with_retry()
        if not bal or 'output1' not in bal: return []
        held = []
        for s in bal['output1']:
            qty = _to_float(s.get('ovrs_cblc_qty', s.get('ccld_qty_smtl1', s.get('hldg_qty', 0))))
            code = s.get('ovrs_pdno', s.get('pdno', ''))
            if qty > 0 and code:
                held.append(code)
        return held
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

def get_kr_company_name(ticker):
    """국장 종목명 조회 (캐시 -> yfinance -> KIS API)"""
    if ticker in kr_name_dict:
        return kr_name_dict[ticker]
    
    name = ticker # Default to ticker
    try:
        # 1. yfinance 시도
        stock_info = yf.Ticker(f"{ticker}.KS").info
        name_yf = stock_info.get('longName', stock_info.get('shortName'))
        if name_yf and name_yf != ticker:
            name = name_yf
        else: # .KS 실패 시 .KQ로 재시도
            stock_info = yf.Ticker(f"{ticker}.KQ").info
            name_yf = stock_info.get('longName', stock_info.get('shortName'))
            if name_yf and name_yf != ticker:
                name = name_yf
    except Exception:
        pass # yfinance 실패 시 KIS로 넘어감

    # 2. yfinance 실패 시 KIS API로 재시도
    if name == ticker and broker_kr:
        try:
            resp = broker_kr.fetch_price(ticker)
            if resp and resp.get('output') and resp['output'].get('prdt_name'):
                name = resp['output']['prdt_name'].strip()
        except Exception:
            pass # KIS도 실패하면 그냥 티커 반환

    kr_name_dict[ticker] = name # 결과 캐싱
    return name

def get_us_company_name(ticker):
    """미장 종목명 조회 (캐시 우선 확인)"""
    if ticker in us_name_dict:
        return us_name_dict[ticker]
    
    name = ticker # Default to ticker
    try:
        # 1. yfinance 시도
        stock_info = yf.Ticker(ticker).info
        name_yf = stock_info.get('longName', stock_info.get('shortName'))
        if name_yf and name_yf != ticker:
            name = name_yf
        else: # .KS 실패 시 .KQ로 재시도
            stock_info = yf.Ticker(f"{ticker}.KQ").info
            name_yf = stock_info.get('longName', stock_info.get('shortName'))
            if name_yf and name_yf != ticker:
                name = name_yf
    except Exception:
        pass # yfinance 실패 시 KIS로 넘어감

    # 2. yfinance 실패 시 KIS API로 재시도
    if name == ticker and broker_kr:
        try:
            resp = broker_kr.fetch_price(ticker)
            if resp and resp.get('output') and resp['output'].get('prdt_name'):
                name = resp['output']['prdt_name'].strip()
        except Exception:
            pass # KIS도 실패하면 그냥 티커 반환

    us_name_dict[ticker] = name # 결과 캐싱
    return name

def get_real_us_positions(broker):
    """[불필요한 토큰 발급 제거] 모지토 기본 토큰 정제기"""
    clean_token = broker.access_token.replace("Bearer ", "").strip()
    is_mock = "vps" in broker.base_url or "vts" in broker.base_url
    tr_id = "VTRP6504R" if is_mock else "CTRP6504R"
    
    url = f"{broker.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance"
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {clean_token}",
        "appkey": broker.api_key.strip(),
        "appsecret": broker.api_secret.strip(),
        "tr_id": tr_id,
        "custtype": "P"
    }
    params = {
        "CANO": broker.acc_no.split("-")[0],
        "ACNT_PRDT_CD": broker.acc_no.split("-")[1],
        "WCRC_FRCR_DVSN_CD": "02", "NATN_CD": "840", "TR_MKET_CD": "00", "INQR_DVSN_CD": "00"
    }
    
    try:
        res = requests.get(url, headers=headers, params=params).json()
        if res.get('rt_cd') != '0':
            print(f"⚠️ [잔고조회 거절됨] {res.get('msg_cd')}: {res.get('msg1')}")
        return res
    except Exception as e:
        print(f"⚠️ 미장 잔고 API 통신 에러: {e}")
        return {}

def get_kis_top_trade_value(broker):
    """한투 API 직통: 실시간 거래대금 상위 100개 스캔"""
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
        pass 
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

def execute_us_order_direct(broker, side, ticker, qty, price):
    """[최종 완전판] 한투 미장 직통 주문기 (거래소 자동분류 + 토큰 정제 + Hashkey)"""
    import requests
    
    is_mock = "vps" in broker.base_url or "vts" in broker.base_url
    if side == "buy": 
        tr_id = "VTTT1002U" if is_mock else "TTTT1002U"
    else: 
        tr_id = "VTTT1001U" if is_mock else "TTTT1006U"
        
    nyse_tickers = {
        "XOM", "CVX", "SLB", "AON", "NOC", "CL", "ICE", "GE", "BA", "DIS", 
        "JNJ", "JPM", "KO", "MCD", "MMM", "PFE", "PG", "UNH", "V", "WMT", 
        "CAT", "TRV", "DOW", "IBM", "HON", "RTX", "AMGN", "SYK", "LMT", "T"
    }
    excg_cd = "NYSE" if ticker in nyse_tickers else "NASD"
    
    url = f"{broker.base_url}/uapi/overseas-stock/v1/trading/order"
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
    
    hash_url = f"{broker.base_url}/uapi/hashkey"
    hash_headers = {
        "content-type": "application/json",
        "appkey": broker.api_key.strip(),
        "appsecret": broker.api_secret.strip()
    }
    hash_res = requests.post(hash_url, headers=hash_headers, json=data).json()
    hashkey = hash_res.get("HASH", "")
    
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
# 5. 기상청 + MDD 브레이크 (올려주신 파일 기반)
# =====================================================================
def get_real_weather(broker_kr, broker_us):
    """V5.0 기관용 기상청: 20일선 기준으로 날씨 판단"""
    weather = {"KR": "☁️ SIDEWAYS", "US": "☁️ SIDEWAYS", "COIN": "☁️ SIDEWAYS"}
    
    # 🇰🇷 국장 날씨 (KODEX 200)
    try:
        ohlcv = broker_kr.fetch_ohlcv("069500")
        if ohlcv and 'output2' in ohlcv:
            closes = [_to_float(x['c']) for x in ohlcv['output2'][::-1]]
            if len(closes) >= 20:
                current = closes[-1]
                ma20 = sum(closes[-20:]) / 20
                if current > ma20 * 1.005:
                    weather['KR'] = "☀️ BULL"
                elif current < ma20 * 0.995:
                    weather['KR'] = "🌧️ BEAR"
    except: pass
    
    # 🇺🇸 미장 날씨 (SPY)
    try:
        resp = broker_us.fetch_ohlcv("SPY", timeframe='D', adj_price=True)
        if resp and 'output2' in resp:
            closes = [_to_float(x['ovrs_nmix_prpr']) for x in reversed(resp['output2'])]
            if len(closes) >= 20:
                current = closes[-1]
                ma20 = sum(closes[-20:]) / 20
                if current > ma20 * 1.005:
                    weather['US'] = "☀️ BULL"
                elif current < ma20 * 0.995:
                    weather['US'] = "🌧️ BEAR"
    except: pass
    
    # 🪙 코인 날씨 (비트코인)
    try:
        df = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=20)
        if df is not None and len(df) >= 20:
            current = df['close'].iloc[-1]
            ma20 = df['close'].mean()
            if current > ma20 * 1.01:
                weather['COIN'] = "☀️ BULL"
            elif current < ma20 * 0.99:
                weather['COIN'] = "🌧️ BEAR"
    except: pass
    
    return weather


# =====================================================================
# GUI용 추가 함수들 (gui_main.py 호환용)
# =====================================================================
def get_held_stocks_kr_info():
    """국내 보유 주식 정보"""
    try:
        bal = get_balance_with_retry()
        if bal and 'output1' in bal:
            return [{'code': s['pdno'], 'name': kr_name_dict.get(s['pdno'], s.get('prdt_name', '')), 'qty': _to_float(s.get('hldg_qty'))} for s in bal['output1'] if _to_float(s.get('hldg_qty')) > 0]
        return []
    except: return []

def get_held_stocks_us_info():
    """미국 보유 주식 정보"""
    try:
        bal = get_us_positions_with_retry()
        if bal and 'output1' in bal:
            return [{'code': s['ovrs_pdno'], 'name': us_name_dict.get(s['ovrs_pdno'], s.get('ovrs_item_name', '')), 'qty': _to_float(s.get('ovrs_cblc_qty'))} for s in bal['output1'] if _to_float(s.get('ovrs_cblc_qty')) > 0]
        return []
    except: return []

def get_held_stocks_us_detail():
    """미국 보유 주식 상세 (GUI용으로 변환)"""
    try:
        bal = get_us_positions_with_retry()
        if not bal or 'output1' not in bal:
            return []
        
        result = []
        for item in bal['output1']:
            qty = _to_float(item.get('ovrs_cblc_qty', item.get('hldg_qty', 0)))
            if qty <= 0:
                qty = _to_float(item.get('ccld_qty_smtl1', 0))
            if qty > 0:
                result.append({
                    'code': item.get('ovrs_pdno', item.get('pdno', '')),
                    'qty': qty,
                    'avg_p': _to_float(item.get('ovrs_avg_pric', item.get('ovrs_avg_unpr', item.get('avg_unpr3', 0))))
                })
        return result
    except:
        return []

def get_held_stocks_coins_info():
    """코인 보유 정보"""
    try:
        balances = upbit.get_balances()
        coins = []
        for b in balances:
            if b['currency'] != 'KRW':
                qty = _to_float(b.get('balance'))
                if qty > 0:
                    ticker = f"KRW-{b['currency']}"
                    price = pyupbit.get_current_price(ticker) or 0
                    coins.append({'code': ticker, 'currency': b['currency'], 'qty': qty, 'current_price': price})
        return coins
    except: return []

def get_safe_balance(data, key=None, default=0):
    """딕셔너리에서 안전하게 값을 추출합니다 (두 가지 사용 방식 지원)
    
    사용 예:
    - get_safe_balance(dict_data, "key_name") → dict에서 key 값 추출
    - get_safe_balance("KR") → market별 잔고 조회 (legacy)
    """
    # legacy: market 조회 모드 (data가 문자열인 경우)
    if isinstance(data, str):
        market = data
        if market == "KR":
            try: return get_balance_with_retry()
            except: return {}
        elif market == "US":
            try: return get_us_positions_with_retry()
            except: return {}
        elif market == "COIN":
            try: return upbit.get_balances()
            except: return []
        return {}
    
    # 현재: dict 값 추출 모드
    if isinstance(data, dict):
        return data.get(key, default)
    return default

def get_balance_with_retry():
    """국내 잔고 조회 (재시도 포함, tr_cont 에러 우회)"""
    try:
        return broker_kr.fetch_balance()
    except KeyError as e:
        if str(e) == "'tr_cont'":
            # mojito 라이브러리의 헤더 버그 우회 - 직접 API 호출
            try:
                access_token = broker_kr.access_token if broker_kr else ''
                cano, prdt_cd = _split_account_no(config.get('kis_account', ''))
                url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/inquire-balance"
                headers = {
                    "content-type": "application/json; charset=utf-8",
                    "authorization": f"Bearer {access_token}",
                    "appkey": config['kis_key'],
                    "appsecret": config['kis_secret'],
                    "tr_id": "TTTC8434R"
                }
                params = {
                    "CANO": cano,
                    "ACNT_PRDT_CD": prdt_cd,
                    "AFHR_FLPR_YN": "N",
                    "OFL_YN": "N",
                    "INQR_DVSN": "01",
                    "UNPR_DVSN": "01",
                    "FUND_STTL_ICLD_YN": "N",
                    "FNCG_AMT_AUTO_RDPT_YN": "N",
                    "PRCS_DVSN": "00",
                    "CTX_AREA_FK100": "",
                    "CTX_AREA_NK100": ""
                }
                res = requests.get(url, headers=headers, params=params)
                return res.json()
            except:
                return {}
        return {}
    except:
        return {}

def get_us_positions_with_retry():
    """미국 포지션 조회 (재시도 포함)"""
    try:
        return get_real_us_positions(broker_us)
    except:
        return {}

def _apply_manual_sell_state_update(ticker, exec_price):
    """수동 매도 체결 후 bot_state의 포지션/승패/누적수익률 반영"""
    state = load_state(STATE_PATH)
    positions = state.setdefault("positions", {})
    pos_info = positions.get(ticker, {})
    buy_p = _to_float(pos_info.get("buy_p", 0), 0.0)

    profit_rate = None
    if buy_p > 0 and _to_float(exec_price, 0.0) > 0:
        profit_rate = ((_to_float(exec_price) - buy_p) / buy_p) * 100
        stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})
        if profit_rate > 0:
            stats["wins"] = int(stats.get("wins", 0) or 0) + 1
        else:
            stats["losses"] = int(stats.get("losses", 0) or 0) + 1
        stats["total_profit"] = float(stats.get("total_profit", 0.0) or 0.0) + float(profit_rate)

    if ticker in positions:
        del positions[ticker]
    set_cooldown(state, ticker)
    save_state(STATE_PATH, state)
    return profit_rate

def manual_sell(market, code, quantity):
    """수동 매도
    반환 형식: {"success": bool, "message": str}
    """
    try:
        qty = _to_float(quantity, 0)
        if qty <= 0:
            return {"success": False, "message": "매도 수량이 0 이하입니다."}

        if market == "KR":
            resp = broker_kr.create_market_sell_order(code, int(qty))
            ok = isinstance(resp, dict) and resp.get("rt_cd") == "0"
            msg = resp.get("msg1", "국장 시장가 매도 요청") if isinstance(resp, dict) else "국장 매도 응답 없음"
            if ok:
                ohlcv = get_ohlcv_realtime(broker_kr, code)
                exec_price = _to_float(ohlcv[-1].get('c', 0), 0.0) if ohlcv else 0.0
                profit_rate = _apply_manual_sell_state_update(code, exec_price)
                _record_trade_event("KR", code, "SELL", int(qty), price=exec_price if exec_price > 0 else None, profit_rate=profit_rate, reason="MANUAL")
                kr_name = get_kr_company_name(code)
                print(f"  ✅ [국장 수동매도 체결] {kr_name}({code}) {int(qty)}주 | 수익률: {profit_rate:+.2f}%")
                send_telegram(f"✅ [KR] {code}({kr_name}) {int(qty)}주 수동 매도 완료")
                return {"success": True, "message": msg}
            return {"success": False, "message": msg}

        if market == "US":
            # 수동매도는 시장가로 처리
            us_bal = ensure_dict(get_us_positions_with_retry())
            current_price = 0.0
            for item in us_bal.get("output1", []) if isinstance(us_bal.get("output1", []), list) else []:
                item_code = item.get("ovrs_pdno", item.get("pdno", ""))
                if item_code == code:
                    current_price = _to_float(item.get("ovrs_nmix_prpr", item.get("ovrs_now_pric1", item.get("ovrs_now_prc2", 0))), 0.0)
                    break
            if current_price <= 0:
                ohlcv_fallback = get_ohlcv_yfinance(code)
                current_price = _to_float(ohlcv_fallback[-1]['c'] if ohlcv_fallback else 0, 0.0)
            if current_price <= 0:
                return {"success": False, "message": "미장 현재가 조회 실패"}

            # 시장가 매도
            resp = execute_us_order_direct(broker_us, "sell", code, int(qty), current_price)
            ok = isinstance(resp, dict) and resp.get("rt_cd") == "0"
            msg = resp.get("msg1", "미장 시장가 매도 요청") if isinstance(resp, dict) else "미장 매도 응답 없음"
            if ok:
                profit_rate = _apply_manual_sell_state_update(code, current_price)
                _record_trade_event("US", code, "SELL", int(qty), price=current_price, profit_rate=profit_rate, reason="MANUAL")
                us_name = get_us_company_name(code)
                print(f"  ✅ [미장 수동매도 체결] {us_name}({code}) {int(qty)}주 | 수익률: {profit_rate:+.2f}%")
                send_telegram(f"✅ [US] {code}({us_name}) {int(qty)}주 수동 매도 완료")
                return {"success": True, "message": msg}
            return {"success": False, "message": msg}

        if market == "COIN":
            current_p = _to_float(pyupbit.get_current_price(code), 0.0)
            resp = upbit.sell_market_order(code, qty)
            if resp:
                profit_rate = _apply_manual_sell_state_update(code, current_p)
                _record_trade_event("COIN", code, "SELL", qty, price=current_p if current_p > 0 else None, profit_rate=profit_rate, reason="MANUAL")
                print(f"  ✅ [코인 수동매도 체결] {code} {qty} | 수익률: {profit_rate:+.2f}%")
                send_telegram(f"✅ [COIN] {code} {qty} 수동 매도 완료")
                return {"success": True, "message": "코인 시장가 매도 요청 완료"}
            return {"success": False, "message": "코인 매도 응답 없음"}

        return {"success": False, "message": f"지원하지 않는 시장 코드: {market}"}
    except Exception as e:
        err = str(e)
        send_telegram(f"🚨 [{market}] {code} 수동 매도 실패: {err}")
        return {"success": False, "message": err}

def _calc_kr_holdings_metrics(balance_data):
    """국내 포지션 지표"""
    if not balance_data or 'output1' not in balance_data:
        return {"invested": 0.0, "current": 0.0, "profit": 0.0, "roi": 0.0}
    try:
        total_invested = 0.0
        total_current = 0.0
        for stock in balance_data['output1']:
            qty = _to_float(stock.get('hldg_qty', stock.get('ccld_qty_smtl1', 0)))
            if qty > 0:
                avg_price = _to_float(stock.get('pchs_avg_prc', stock.get('pchs_avg_pric', 0)))
                invested = avg_price * qty
                current_price = _to_float(stock.get('prpr', stock.get('stck_prpr', 0)))
                current = current_price * qty
                total_invested += invested
                total_current += current
        profit = total_current - total_invested
        roi = (profit / total_invested * 100) if total_invested > 0 else 0.0
        return {"invested": total_invested, "current": total_current, "profit": profit, "roi": roi}
    except: return {"invested": 0.0, "current": 0.0, "profit": 0.0, "roi": 0.0}

def _calc_us_holdings_metrics(balance_data):
    """미국 포지션 지표"""
    if not balance_data or 'output1' not in balance_data:
        return {"invested": 0.0, "current": 0.0, "profit": 0.0, "roi": 0.0}
    try:
        total_invested = 0.0
        total_current = 0.0
        for stock in balance_data['output1']:
            qty = _to_float(stock.get('ovrs_cblc_qty', stock.get('hldg_qty', 0)))
            if qty <= 0:
                qty = _to_float(stock.get('ccld_qty_smtl1', 0))
            if qty > 0:
                avg_price = _to_float(stock.get('ovrs_avg_unpr', stock.get('ovrs_avg_pric', stock.get('avg_unpr3', 0))))
                invested = avg_price * qty
                current_price = _to_float(stock.get('ovrs_now_prc2', stock.get('ovrs_nmix_prpr', stock.get('ovrs_now_pric1', 0))))
                current = current_price * qty
                total_invested += invested
                total_current += current
        profit = total_current - total_invested
        roi = (profit / total_invested * 100) if total_invested > 0 else 0.0
        return {"invested": total_invested, "current": total_current, "profit": profit, "roi": roi}
    except: return {"invested": 0.0, "current": 0.0, "profit": 0.0, "roi": 0.0}

def _calc_coin_holdings_metrics(balances):
    """코인 포지션 지표"""
    if not balances:
        return {"invested": 0.0, "current": 0.0, "profit": 0.0, "roi": 0.0}
    try:
        total_current = 0.0
        for b in balances:
            if b['currency'] != 'KRW':
                qty = _to_float(b.get('balance', 0))
                if qty > 0:
                    ticker = f"KRW-{b['currency']}"
                    price = pyupbit.get_current_price(ticker) or 0
                    current = qty * price
                    total_current += current
        return {"invested": 0.0, "current": total_current, "profit": 0.0, "roi": 0.0}
    except: return {"invested": 0.0, "current": 0.0, "profit": 0.0, "roi": 0.0}

def set_cooldown(state, code):
    """쿨다운 설정"""
    state.setdefault("cooldown", {})[code] = datetime.now().isoformat(timespec="seconds")


def _get_live_position_seeds():
    """실계좌 보유종목의 진입 기준가(평단) 시드 수집
    - 국장/미장: API 응답의 평균단가 우선
    - 코인: avg_buy_price 사용
    """
    seeds = {}

    # ===== 🇰🇷 국장 =====
    try:
        kr_bal = ensure_dict(get_balance_with_retry())
        output1 = kr_bal.get('output1', []) if isinstance(kr_bal.get('output1', []), list) else []
        for stock in output1:
            code = stock.get('pdno', '')
            qty = _to_float(stock.get('hldg_qty', stock.get('ccld_qty_smtl1', 0)))
            if not code or qty <= 0:
                continue
            avg_p = _to_float(stock.get('pchs_avg_prc', stock.get('pchs_avg_pric', 0)))
            if avg_p <= 0:
                avg_p = _to_float(stock.get('prpr', stock.get('stck_prpr', 0)))
            if avg_p > 0:
                seeds[code] = avg_p
    except Exception:
        pass

    # ===== 🇺🇸 미장 =====
    try:
        us_bal = ensure_dict(get_us_positions_with_retry())
        output1 = us_bal.get('output1', []) if isinstance(us_bal.get('output1', []), list) else []
        for stock in output1:
            code = stock.get('ovrs_pdno', stock.get('pdno', ''))
            qty = _to_float(stock.get('ovrs_cblc_qty', stock.get('ccld_qty_smtl1', stock.get('hldg_qty', 0))))
            if not code or qty <= 0:
                continue
            avg_p = _to_float(stock.get('ovrs_avg_unpr', stock.get('ovrs_avg_pric', stock.get('avg_unpr3', 0))))
            if avg_p <= 0:
                avg_p = _to_float(stock.get('ovrs_now_prc2', stock.get('ovrs_nmix_prpr', stock.get('ovrs_now_pric1', 0))))
            if avg_p > 0:
                seeds[code] = avg_p
    except Exception:
        pass

    # ===== 🪙 코인 =====
    try:
        balances = upbit.get_balances() or []
        if isinstance(balances, list):
            for b in balances:
                currency = b.get('currency')
                if currency in ['KRW', 'VTHO']:
                    continue
                qty = _to_float(b.get('balance', 0))
                if qty <= 0.00000001:
                    continue
                ticker = f"KRW-{currency}"
                avg_p = _to_float(b.get('avg_buy_price', 0))
                if avg_p <= 0:
                    avg_p = _to_float(pyupbit.get_current_price(ticker), 0)
                if avg_p > 0:
                    seeds[ticker] = avg_p
    except Exception:
        pass

    return seeds


def sync_all_positions(state, held_kr, held_us, held_coins):
    """국장/미장/코인 통합 장부 정리
    1) 실보유인데 장부에 없는 종목은 즉시 등록
    2) 장부에만 있고 실보유가 아닌 유령종목 삭제
    """
    print(f"🔄 [장부 점검] 실제 잔고 (국장:{len(held_kr)} / 미장:{len(held_us)} / 코인:{len(held_coins)}) 대조 중...")
    if "positions" not in state:
        state["positions"] = {}

    current_positions = state["positions"]
    changes_made = False
    recovered_count = 0

    # -----------------------------------------------------------------
    # 1) 자동 복구: 실보유인데 bot_state 장부에 없는 종목은 즉시 등록
    #    - buy_p: 실계좌 평단(또는 대체 현재가)
    #    - sl_p: V5.0 로직 (매수가 - 2.5 * ATR)
    # -----------------------------------------------------------------
    live_seeds = _get_live_position_seeds()
    for ticker, buy_p in live_seeds.items():
        if ticker not in current_positions:
            # V5.0 손절가 자동 계산
            ohlcv = get_ohlcv_yfinance(ticker)
            atr = calculate_atr(ohlcv)
            sl_p = buy_p - (atr * 2.5) if atr > 0 else buy_p * 0.90
            tier = "자동복구(V5.0손절-매수가)" if atr > 0 else "자동복구(-10%손절)"

            current_positions[ticker] = {
                "buy_p": float(buy_p),
                "sl_p": float(sl_p),
                "max_p": float(buy_p),
                "tier": tier,
            }
            # 종목명 가져오기
            if ticker.isdigit():
                name = get_kr_company_name(ticker)
            elif ticker.startswith("KRW-"):
                name = ticker
            else:
                name = get_us_company_name(ticker)
            print(f"  -> 🚨 [자동복구] {name}({ticker}) 장부 등록 완료 (평단={buy_p:.4f}, 손절={sl_p:.4f})")
            changes_made = True
            recovered_count += 1

    # -----------------------------------------------------------------
    # 2) 유령 제거: 장부에만 있고 계좌에 없는 종목은 삭제
    # -----------------------------------------------------------------
    to_delete = []

    for ticker in list(current_positions.keys()):
        if ticker.isdigit():
            if ticker not in held_kr:
                to_delete.append(ticker)
        elif ticker.startswith("KRW-"):
            if ticker not in held_coins:
                to_delete.append(ticker)
        else:
            if ticker not in held_us:
                to_delete.append(ticker)

    for t in to_delete:
        # 종목명 가져오기
        if t.isdigit():
            name = get_kr_company_name(t)
        elif t.startswith("KRW-"):
            name = t
        else:
            name = get_us_company_name(t)
        print(f"  -> 🧹 [통합 장부정리] 계좌에 없는 {name}({t}) 발견! 메모장에서 삭제했습니다.")
        del state["positions"][t]
        changes_made = True

    if changes_made:
        save_state(STATE_PATH, state)
        print(f"  -> ✅ 장부 동기화 완료 (복구 {recovered_count} / 유령정리 {len(to_delete)})")
        return True
    return False



def get_kr_holdings_with_roi():
    """🇰🇷 국장 보유 종목 + 현재 수익률 (balance API 현재가 사용)"""
    try:
        state = load_state(STATE_PATH)
        bal = ensure_dict(get_balance_with_retry())
        kr_output1 = bal.get('output1', []) if isinstance(bal.get('output1'), list) else []
        
        holdings = []
        for stock in kr_output1:
            code = stock.get('pdno', '')
            if not code:
                continue
            qty = int(_to_float(stock.get('hldg_qty', 0)))
            if qty <= 0:
                continue
            
            pos = state.get('positions', {}).get(code, {})
            buy_p = _to_float(pos.get('buy_p', 0), 0)
            if buy_p <= 0:
                continue
            
            # 현재가: balance API에서 직접 (prpr = 현재가, 가장 빠름)
            curr_p = float(_to_float(stock.get('prpr', buy_p), buy_p))
                
            roi = ((curr_p - buy_p) / buy_p) * 100
            kr_name = get_kr_company_name(code)
            holdings.append(f"  {code}({kr_name}): {int(curr_p):,}원 | {roi:+.2f}%")
        
        return holdings
    except:
        return []

def get_us_holdings_with_roi():
    """🇺🇸 미장 보유 종목 + 현재 수익률"""
    try:
        state = load_state(STATE_PATH)
        # GUI와 동일한 함수 사용
        us_data = get_held_stocks_us_detail()
        if not us_data:
            return []
        
        holdings = []
        for item in us_data:
            ticker = item['code']
            qty = item['qty']
            buy_p = item['avg_p']
            
            if buy_p <= 0:
                continue
            
            # 현재가: yfinance → fallback으로 get_ohlcv_yfinance
            curr_p = buy_p
            try:
                import yfinance as yf
                ticker_info = yf.Ticker(ticker)
                curr_p = ticker_info.info.get('currentPrice')
                if not curr_p:
                    ohlcv = get_ohlcv_yfinance(ticker)
                    if ohlcv and len(ohlcv) > 0:
                        curr_p = float(ohlcv[-1]['c'])
                    else:
                        curr_p = buy_p
            except:
                # yfinance 실패시 get_ohlcv_yfinance 사용
                try:
                    ohlcv = get_ohlcv_yfinance(ticker)
                    if ohlcv and len(ohlcv) > 0:
                        curr_p = float(ohlcv[-1]['c'])
                except:
                    curr_p = buy_p
            
            roi = ((curr_p - buy_p) / buy_p) * 100
            us_name = get_us_company_name(ticker)
            holdings.append(f"  {ticker}({us_name}): ${curr_p:.2f} | {roi:+.2f}%")
        
        return holdings
    except Exception as e:
        print(f"⚠️ US 보유종목 조회 에러: {e}")
        return []

def heartbeat_report():
    """모든 자산 현황을 종합하여 텔레그램으로 보고 (GUI와 동일한 로직)"""
    print("💓 생존 신고 보고서 생성 중...")
    try:
        # 시장 날씨 조회 (20일선 기준)
        weather = get_real_weather(broker_kr, broker_us)
        
        # ===== 국장 =====
        kr_cash = 0
        kr_total = 0
        kr_roi = None
        try:
            kr_bal = get_balance_with_retry()
            if kr_bal is None:
                kr_bal = {}
            
            # 국장 예수금 (GUI와 동일: prvs_rcdl_excc_amt)
            if 'output2' in kr_bal:
                out2 = kr_bal['output2']
                if isinstance(out2, list) and len(out2) > 0:
                    kr_cash = int(_to_float(out2[0].get('prvs_rcdl_excc_amt', 0)))
                elif isinstance(out2, dict):
                    kr_cash = int(_to_float(out2.get('prvs_rcdl_excc_amt', 0)))
            
            # 국장 총평가 (GUI와 동일 로직)
            kr_metrics = _calc_kr_holdings_metrics(kr_bal)
            kr_roi = kr_metrics.get("roi")
            
            try:
                out2 = kr_bal.get("output2", [])
                if isinstance(out2, list) and out2:
                    kr_total = int(_to_float(out2[0].get("tot_evlu_amt"), kr_cash))
                elif isinstance(out2, dict):
                    kr_total = int(_to_float(out2.get("tot_evlu_amt"), kr_cash))
            except:
                kr_total = None
            
            if kr_total is None:
                kr_total = int(kr_cash + float(kr_metrics.get("current", 0.0)))
        except Exception as e:
            print(f"  ⚠️ 국장 조회 실패: {e}")
            kr_cash = 0
            kr_total = 0
        
        # ===== 미장 =====
        us_cash = 0.0
        us_total = 0.0
        us_roi = None
        try:
            us_cash = _safe_num(get_us_cash_real(broker_us), 0.0)
            us_bal = get_us_positions_with_retry() or {}
            us_metrics = _calc_us_holdings_metrics(us_bal)
            us_roi = us_metrics.get("roi")
            
            # GUI의 fallback 로직: cash가 0이면 output2에서 재추출
            if us_cash <= 0 and isinstance(us_bal, dict):
                out2 = us_bal.get("output2", [])
                if isinstance(out2, list) and out2:
                    us_cash = _safe_num(out2[0].get("frcr_dncl_amt_2", out2[0].get("frcr_buy_amt_smtl", 0)), 0.0)
                elif isinstance(out2, dict):
                    us_cash = _safe_num(out2.get("frcr_dncl_amt_2", out2.get("frcr_buy_amt_smtl", 0)), 0.0)
            
            # 미장 총평가 = 예수금 + 보유종목 평가액
            us_stock_value = float(us_metrics.get("current", 0.0) or 0.0)
            us_total = us_cash + us_stock_value
        except Exception as e:
            print(f"  ⚠️ 미장 조회 실패: {e}")
            us_cash = 0.0
            us_total = 0.0
        
        # ===== 코인 =====
        krw_bal = 0
        coin_total = 0
        coin_roi = None
        try:
            krw_bal = int(_safe_num(upbit.get_balance("KRW"), 0.0))
            coin_bals = upbit.get_balances() or []
            
            coin_metrics = _calc_coin_holdings_metrics(coin_bals)
            coin_roi = coin_metrics.get("roi")
            
            # 코인 총평가 = KRW + 코인 평가액
            coin_value = float(coin_metrics.get("current", 0.0) or 0.0)
            coin_total = int(krw_bal + coin_value)
        except Exception as e:
            print(f"  ⚠️ 코인 조회 실패: {e}")
            coin_total = krw_bal
        
        # 수익률 텍스트 포맷팅
        kr_roi_str = f"{kr_roi:+.2f}%" if kr_roi is not None else "보유없음"
        us_roi_str = f"{us_roi:+.2f}%" if us_roi is not None else "보유없음"
        coin_roi_str = f"{coin_roi:+.2f}%" if coin_roi is not None else "보유없음"
        
        # 보유 종목 및 수익률
        kr_holdings = get_kr_holdings_with_roi()
        us_holdings = get_us_holdings_with_roi()
        
        kr_holdings_str = "\n".join(kr_holdings) if kr_holdings else "  (보유 없음)"
        us_holdings_str = "\n".join(us_holdings) if us_holdings else "  (보유 없음)"
        
        msg = f"""💓 [3콤보 생존신고]
{weather['KR']} 🇰🇷 국장 | 예수금: {kr_cash:,}원 | 총평가: {kr_total:,}원 | 수익률: {kr_roi_str}
[국장 보유]
{kr_holdings_str}

{weather['US']} 🇺🇸 미장 | 예수금: {us_cash:,.2f}$ | 총평가: {us_total:,.2f}$ | 수익률: {us_roi_str}
[미장 보유]
{us_holdings_str}

{weather['COIN']} 🪙 코인 | 예수금: {krw_bal:,}원 | 총평가: {coin_total:,}원 | 수익률: {coin_roi_str}"""
        send_telegram(msg)
        print("  ✅ 텔레그램 보고 완료")
    except Exception as e:
        print(f"⚠️ 보고 에러: {e}")
        import traceback
        traceback.print_exc()

# =====================================================================
# 6. 메인 매매 엔진 (올려주신 파일 기반 - 대폭 단순화 버전)
# =====================================================================
def run_trading_bot():
    """15분마다 실행되는 통합 메인 엔진 (main641 로직 복원판)"""
    print("\n" + "="*55)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🤖 V5.0 통합 자동매매 봇 가동...")
    print("="*55)

    global broker_kr, broker_us, upbit

    state = load_state(STATE_PATH)
    refresh_brokers_if_needed()

    # 0) 실보유/장부 동기화 (누락 종목 자동복구 + 유령 삭제)
    held_kr = get_held_stocks_kr()
    held_us = get_held_stocks_us()
    held_coins = get_held_coins()
    sync_all_positions(state, held_kr, held_us, held_coins)
    weather = get_real_weather(broker_kr, broker_us)
    print(f"🌡️ 시장 날씨: 국장 {weather['KR']} / 미장 {weather['US']} / 코인 {weather['COIN']}")

    try:
        with open(BASE_DIR / "kr_targets.json", "r", encoding="utf-8") as f:
            scanned_targets = json.load(f)
    except Exception:
        scanned_targets = []

    # 1) 국장 타겟 구성 (main641 동일 구조: 시총 + 거래대금 + 사용자 타겟)
    market_cap_200 = get_kis_market_cap_rank(broker_kr, limit=200)
    realtime_trade_all = get_kis_top_trade_value(broker_kr)
    tier_1 = [t for t in realtime_trade_all[:50] if t in market_cap_200]
    tier_2 = [t for t in realtime_trade_all[:50] if t not in tier_1]
    tier_3 = list(dict.fromkeys(scanned_targets + market_cap_200))

    final_targets = []
    seen = set()
    for t in (tier_1 + tier_2 + tier_3):
        if t not in seen:
            final_targets.append(t)
            seen.add(t)
    print(f"  -> 🌐 [국장 타겟] 1티어({len(tier_1)}개) 포함 총 {len(final_targets)}개")

    # 2) 미장 고정 타겟 (main641 확장 리스트)
    night_targets = [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK-B", "LLY", "AVGO",
        "JPM", "V", "XOM", "UNH", "MA", "PG", "JNJ", "HD", "MRK", "COST",
        "ABBV", "CVX", "CRM", "AMD", "NFLX", "KO", "PEP", "BAC", "TMO", "WMT",
        "ACN", "LIN", "MCD", "CSCO", "ABT", "INTC", "QCOM", "INTU", "VZ", "CMCSA",
        "TXN", "DHR", "PFE", "AMAT", "UNP", "IBM", "NOW", "COP", "PM", "BA",
        "ISRG", "GE", "HON", "CAT", "RTX", "DIS", "AMGN", "SYK", "LMT", "T",
        "SPGI", "BKNG", "BLK", "MDLZ", "TJX", "ADP", "PGR", "C", "REGN", "VRTX",
        "ADI", "MMC", "CB", "BSX", "CI", "CVS", "ZTS", "PANW", "SNPS",
        "KLAC", "GILD", "CDNS", "EQIX", "WM", "CSX", "CME", "MO", "ITW", "SHW",
        "DUK", "SO", "BDX", "MCO", "EOG", "SLB", "AON", "NOC", "CL", "ICE"
    ]

    if is_market_open("KR"):
        print("▶️ [🇰🇷 국장] 매매 엔진 시작...")
        bal = ensure_dict(get_balance_with_retry())
        kr_balance_data = bal.get('output2', [])
        if isinstance(kr_balance_data, list) and kr_balance_data:
            kr_cash = int(_to_float(kr_balance_data[0].get('prvs_rcdl_excc_amt', 0)))
            total_kr_equity = int(_to_float(kr_balance_data[0].get('tot_evlu_amt', kr_cash)))
        elif isinstance(kr_balance_data, dict):
            kr_cash = int(_to_float(kr_balance_data.get('prvs_rcdl_excc_amt', 0)))
            total_kr_equity = int(_to_float(kr_balance_data.get('tot_evlu_amt', kr_cash)))
        else:
            kr_cash, total_kr_equity = 0, 0

        kr_output1 = bal.get('output1', []) if isinstance(bal.get('output1', []), list) else []
        if not check_mdd_break("KR", total_kr_equity, state, STATE_PATH):
            print("  -> 🚨 국장 MDD 브레이크 작동 중. 신규 매수 중단.")
        else:
            for stock in kr_output1:
                t = stock.get('pdno', '')
                if t in CORE_ASSETS or t not in state.get("positions", {}):
                    continue
                try:
                    ohlcv = get_ohlcv_realtime(broker_kr, t)
                    if not ohlcv:
                        continue
                    curr_p = float(ohlcv[-1]['c'])
                    pos_info = state.get("positions", {}).get(t, {})
                    buy_p = pos_info.get('buy_p', curr_p)
                    pos_info['max_p'] = max(pos_info.get('max_p', buy_p), curr_p)
                    state.setdefault("positions", {})[t] = pos_info
                    if curr_p < buy_p * 1.01:
                        continue
                    is_exit, reason = check_pro_exit(curr_p, pos_info, ohlcv)
                    if is_exit:
                        kr_name = get_kr_company_name(t)  # 종목명 미리 조회
                        qty = int(_to_float(stock.get('hldg_qty', stock.get('ccld_qty_smtl1', 0))))
                        if qty <= 0:
                            continue
                        
                        # 매도 주문 (최대 3회 재시도)
                        retry_count = 0
                        max_retries = 3
                        resp = None
                        
                        while retry_count < max_retries:
                            resp = broker_kr.create_market_sell_order(t, qty)
                            if resp.get('rt_cd') == '0':
                                break
                            retry_count += 1
                            if retry_count < max_retries:
                                print(f"  ⚠️ {kr_name}({t}) 매도 실패 (#{retry_count}): {resp.get('msg1', 'API 오류')} → 재시도")
                                import time
                                time.sleep(1)
                        
                        if resp and resp.get('rt_cd') == '0':
                            profit_rate = ((curr_p - buy_p) / buy_p) * 100 if buy_p > 0 else 0.0
                            stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})
                            if profit_rate > 0:
                                stats["wins"] += 1
                            else:
                                stats["losses"] += 1
                            stats["total_profit"] = float(stats.get("total_profit", 0.0) or 0.0) + float(profit_rate)
                            _record_trade_event("KR", t, "SELL", qty, price=curr_p, profit_rate=profit_rate, reason=reason)
                            print(f"  ✅ [국장 매도 체결] {kr_name}({t}) | 수익률: {profit_rate:+.2f}% | 사유: {reason}")
                            send_telegram(f"🚨 [국장 추세종료 매도] {t}({kr_name})\n사유: {reason}\n최종 수익률: {profit_rate:.2f}%")
                            del state["positions"][t]
                            set_cooldown(state, t)
                            save_state(STATE_PATH, state)
                        else:
                            print(f"  ❌ {kr_name}({t}) 매도 최종 실패 ({retry_count}회 시도): {resp.get('msg1', 'API 오류') if resp else '응답 없음'}")
                except Exception:
                    continue

            total_kr = len(final_targets)
            print(f"  -> 🇰🇷 국장 사냥감 {total_kr}개 정밀 분석 시작!")
            for idx, t in enumerate(final_targets, 1):
                kr_name = get_kr_company_name(t)  # 종목명 미리 조회
                # 핵심자산: cooldown 무시, 중복 매수 가능
                if t in CORE_ASSETS:
                    pass  # 핵심자산은 모든 제약 무시
                else:
                    # 일반 종목: cooldown 및 보유 여부 체크
                    if in_cooldown(state, t):
                        print(f"  ⏭️ {kr_name}({t}): 쿨다운 중 (패스)")
                        continue
                    if t in held_kr:
                        print(f"  ⏭️ {kr_name}({t}): 이미 보유중 (패스)")
                        continue
                
                try:
                    ohlcv_200 = get_ohlcv_yfinance(t)
                    is_buy, sl_p, s_name = calculate_pro_signals(ohlcv_200, weather['KR'], t, kr_name, idx, total_kr)
                    if not is_buy:
                        continue
                    
                    # 갭업 판별 (yfinance 데이터 사용 - API 호출 없음)
                    if ohlcv_200 and len(ohlcv_200) >= 2:
                        prev_close = float(ohlcv_200[-2]['c'])
                        today_open = float(ohlcv_200[-1]['o'])
                        gap_up_rate = ((today_open - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
                        if gap_up_rate >= 5.0:
                            print(f"  ⏭️ {kr_name}({t}): 갭업 과다 ({gap_up_rate:.2f}% >= 5%) (패스)")
                            continue
                    
                    if weather['KR'] == "☀️ BULL":
                        if t in tier_1:
                            ratio, t_name = 0.60, "1티어(우량대장)-불장"
                        elif t in tier_2:
                            ratio, t_name = 0.40, "2티어(수급급등)-불장"
                        else:
                            ratio, t_name = 0.30, "3티어(기타/패턴)-불장"
                    elif weather['KR'] == "☁️ SIDEWAYS":
                        if t in tier_1:
                            ratio, t_name = 0.40, "1티어(우량대장)-횡보"
                        elif t in tier_2:
                            ratio, t_name = 0.30, "2티어(수급급등)-횡보"
                        else:
                            ratio, t_name = 0.20, "3티어(기타/패턴)-횡보"
                    else:
                        ratio, t_name = 0.10, "기타-방어"
                    target_budget = total_kr_equity * ratio
                    if not can_open_new(t, state, max_positions=8):
                        print(f"  ⏭️ {kr_name}({t}): 포지션 개수 초과 (8개) (패스)")
                        continue
                    if target_budget <= 50000:
                        print(f"  ⏭️ {kr_name}({t}): 목표 예산 너무 적음 ({int(target_budget):,}원 <= 50,000원) (패스)")
                        continue
                    if kr_cash < target_budget:
                        print(f"  ⏭️ {kr_name}({t}): 예수금 부족 (현재: {int(kr_cash):,}원 < 필요: {int(target_budget):,}원) (패스)")
                        continue
                    
                    # 현재가: yfinance 데이터 사용 (이미 조회했으니 추가 API 없음)
                    curr_p = 0.0
                    if ohlcv_200 and len(ohlcv_200) > 0:
                        curr_p = float(ohlcv_200[-1]['c'])
                    
                    if curr_p <= 0:
                        print(f"  ⏭️ {kr_name}({t}): 현재가 조회 실패 (패스)")
                        continue
                    qty = int(target_budget / curr_p)
                    if qty <= 0:
                        print(f"  ⏭️ {kr_name}({t}): 매수 수량 계산 실패 (패스)")
                        continue
                    
                    # 매수 주문
                    resp = broker_kr.create_market_buy_order(t, qty)
                    if resp.get('rt_cd') == '0':
                        print(f"  ✅ [국장 매수 체결] {kr_name}({t}) | {int(curr_p):,}원 × {qty}주 | 손절가: {int(sl_p):,}원")
                        send_telegram(f"🎯 [{t_name} 매수] {t}({kr_name})\n평단가: {int(curr_p):,}원 × {qty}주 | 손절가: {int(sl_p):,}원\n전략: {s_name}")
                        kr_cash -= (qty * curr_p)
                        state.setdefault("positions", {})[t] = {'buy_p': curr_p, 'sl_p': sl_p, 'max_p': curr_p, 'tier': t_name}
                        _record_trade_event("KR", t, "BUY", qty, price=curr_p, profit_rate=None, reason=s_name)
                        save_state(STATE_PATH, state)
                    else:
                        print(f"  ❌ {kr_name}({t}) 매수 실패: {resp.get('msg1', 'API 오류')} (rt_cd: {resp.get('rt_cd')})")
                except Exception:
                    continue
    else:
        print("💤 국장은 현재 휴장 상태입니다.")

    if is_market_open("US"):
        print("▶️ [🇺🇸 미장] 매매 엔진 시작...")
        us_cash = float(get_us_cash_real(broker_us) or 0.0)
        us_bal = ensure_dict(get_us_positions_with_retry())
        out2 = safe_get(us_bal, 'output2', {})
        us_stock_value = float(out2.get('ovrs_stck_evlu_amt', 0)) if isinstance(out2, dict) else 0.0
        total_us_equity = us_cash + us_stock_value
        us_output1 = safe_get(us_bal, 'output1', [])

        if not check_mdd_break("US", total_us_equity, state, STATE_PATH):
            print("  -> 🚨 미장 MDD 브레이크 작동 중. 신규 매수 중단.")
        else:
            for stock in us_output1:
                t = stock.get('ovrs_pdno', '')
                if t in CORE_ASSETS or t not in state.get("positions", {}):
                    continue
                try:
                    ohlcv = get_ohlcv_yfinance(t)
                    if not ohlcv:
                        continue
                    curr_p = float(ohlcv[-1]['c'])
                    pos_info = state.get("positions", {}).get(t, {})
                    buy_p = pos_info.get('buy_p', curr_p)
                    pos_info['max_p'] = max(pos_info.get('max_p', buy_p), curr_p)
                    state.setdefault("positions", {})[t] = pos_info
                    if curr_p < buy_p * 1.01:
                        continue
                    is_exit, reason = check_pro_exit(curr_p, pos_info, ohlcv)
                    if is_exit:
                        us_name = get_us_company_name(t)  # 종목명 미리 조회
                        qty = float(_to_float(stock.get('ovrs_cblc_qty', stock.get('hldg_qty', 0))))
                        if qty <= 0:
                            continue
                        
                        # 시장가 매도 (98% 지정가 = 즉시 체결 + 가격 보호)
                        sell_price = round(curr_p * 0.98, 2)
                        
                        # 최대 3회 재시도
                        retry_count = 0
                        max_retries = 3
                        resp = None
                        
                        while retry_count < max_retries:
                            resp = execute_us_order_direct(broker_us, "sell", t, qty, sell_price)
                            if resp.get('rt_cd') == '0':
                                break
                            retry_count += 1
                            if retry_count < max_retries:
                                print(f"  ⚠️ {us_name}({t}) 매도 실패 (#{retry_count}): {resp.get('msg1', 'API 오류')} → 재시도")
                                import time
                                time.sleep(1)  # 1초 대기 후 재시도
                        
                        if resp and resp.get('rt_cd') == '0':
                            profit_rate = ((sell_price - buy_p) / buy_p) * 100 if buy_p > 0 else 0.0
                            stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})
                            if profit_rate > 0:
                                stats["wins"] += 1
                            else:
                                stats["losses"] += 1
                            stats["total_profit"] = float(stats.get("total_profit", 0.0) or 0.0) + float(profit_rate)
                            _record_trade_event("US", t, "SELL", qty, price=sell_price, profit_rate=profit_rate, reason=reason)
                            print(f"  ✅ [미장 매도 체결] {us_name}({t}) | 수익률: {profit_rate:+.2f}% | 사유: {reason}")
                            send_telegram(f"🚨 [미장 추세종료 매도] {t}({us_name})\n사유: {reason}\n최종 수익률: {profit_rate:.2f}%")
                            del state["positions"][t]
                            set_cooldown(state, t)
                            save_state(STATE_PATH, state)
                        else:
                            print(f"  ❌ {us_name}({t}) 매도 최종 실패 ({retry_count}회 시도): {resp.get('msg1', 'API 오류')}")
                except Exception:
                    continue

            if weather['US'] != "🌧️ BEAR":
                total_us = len(night_targets)
                print(f"  -> 🇺🇸 미장 대장주 {total_us}개 정밀 분석 시작!")
                if weather['US'] == "☀️ BULL":
                    target_budget = us_cash * 0.50
                elif weather['US'] == "☁️ SIDEWAYS":
                    target_budget = us_cash * 0.40
                else:
                    target_budget = us_cash * 0.30

                for idx, t in enumerate(night_targets, 1):
                    try:
                        us_name = get_us_company_name(t)  # 종목명 미리 조회
                        ohlcv = get_ohlcv_yfinance(t)
                        is_buy, sl_p, s_name = calculate_pro_signals(ohlcv, weather['US'], t, us_name, idx, total_us)
                        if not is_buy:
                            continue
                        if not can_open_new(t, state, max_positions=5):
                            print(f"  ⏭️ {us_name}({t}): 포지션 개수 초과 (5개) (패스)")
                            continue
                        if target_budget <= 200:
                            print(f"  ⏭️ {us_name}({t}): 목표 예산 너무 적음 (${target_budget:.2f} <= $200) (패스)")
                            continue
                        if us_cash < target_budget:
                            print(f"  ⏭️ {us_name}({t}): 예수금 부족 (현재: ${us_cash:.2f} < 필요: ${target_budget:.2f}) (패스)")
                            continue
                        if not ohlcv:
                            print(f"  ⏭️ {us_name}({t}): OHLCV 데이터 부족 (패스)")
                            continue
                        curr_p = float(ohlcv[-1]['c'])
                        qty = int(target_budget / curr_p) if curr_p > 0 else 0
                        if qty > 0:
                            # 시장가 매수 (101% 지정가 = 즉시 체결 + 가격 보호)
                            buy_price = round(curr_p * 1.01, 2)
                            
                            # 최대 3회 재시도
                            retry_count = 0
                            max_retries = 3
                            resp = None
                            
                            while retry_count < max_retries:
                                resp = execute_us_order_direct(broker_us, "buy", t, qty, buy_price)
                                if resp.get('rt_cd') == '0':
                                    break
                                retry_count += 1
                                if retry_count < max_retries:
                                    print(f"  ⚠️ {us_name}({t}) 매수 실패 (#{retry_count}): {resp.get('msg1', 'API 오류')} → 재시도")
                                    import time
                                    time.sleep(1)
                            
                            if resp and resp.get('rt_cd') == '0':
                                print(f"  ✅ [미장 매수 체결] {us_name}({t}) | ${curr_p:.2f} × {qty}주 | 손절가: ${sl_p:.2f}")
                                send_telegram(f"🎯 [미장 V5.0 기관매수] {t}({us_name})\n평단가: ${curr_p:.2f} × {qty}주 | 손절가: ${sl_p:.2f}\n전략: {s_name}")
                                us_cash -= (qty * curr_p)
                                state.setdefault("positions", {})[t] = {'buy_p': curr_p, 'sl_p': sl_p, 'max_p': curr_p}
                                _record_trade_event("US", t, "BUY", qty, price=buy_price, profit_rate=None, reason=s_name)
                                set_cooldown(state, t)
                                save_state(STATE_PATH, state)
                                held_coins.append(t)
                                krw_bal -= budget
                            else:
                                print(f"  ❌ {us_name}({t}) 매수 최종 실패 ({retry_count}회 시도): {resp.get('msg1', 'API 오류') if resp else '응답 없음'}")
                    except Exception:
                        continue
    else:
        print("💤 미장은 현재 휴장 상태입니다.")

    if is_market_open("COIN"):
        coin_weather = weather.get('COIN', '☁️ SIDEWAYS')
        print("▶️ [🪙 코인] 매매 엔진 시작...")
        balances = upbit.get_balances() or []
        krw_bal = float(next((b.get('balance', 0) for b in balances if b.get('currency') == 'KRW'), 0) or 0)
        held_coins = [f"KRW-{b['currency']}" for b in balances if b.get('currency') not in ['KRW', 'VTHO'] and float(_to_float(b.get('avg_buy_price', 0))) > 0]

        total_coin_equity = krw_bal
        for b in balances:
            if b.get('currency') not in ['KRW', 'VTHO']:
                curr_p = pyupbit.get_current_price(f"KRW-{b['currency']}")
                if curr_p:
                    total_coin_equity += float(_to_float(b.get('balance', 0))) * float(curr_p)

        if not check_mdd_break("COIN", total_coin_equity, state, STATE_PATH):
            print("  -> 🚨 코인 MDD 브레이크 작동 중. 신규 매수 중단.")
        else:
            for b in balances:
                if b.get('currency') in ['KRW', 'VTHO']:
                    continue
                t = f"KRW-{b['currency']}"
                if t not in state.get("positions", {}):
                    print(f"  ⏭️ {t}: 장부에 없음 (패스)")
                    continue
                qty = float(_to_float(b.get('balance', 0)))
                if qty <= 0.0001:
                    print(f"  ⏭️ {t}: 수량 너무 적음 ({qty}) (패스)")
                    continue
                curr_p = pyupbit.get_current_price(t)
                if not curr_p:
                    print(f"  ⏭️ {t}: 현재가 조회 실패 (패스)")
                    continue
                df_upbit = pyupbit.get_ohlcv(t, interval="day", count=250)
                if df_upbit is None or len(df_upbit) < 20:
                    print(f"  ⏭️ {t}: OHLCV 데이터 부족 (패스)")
                    continue
                ohlcv = [{'o': row['open'], 'h': row['high'], 'l': row['low'], 'c': row['close'], 'v': row['volume']} for _, row in df_upbit.iterrows()]
                pos_info = state.get("positions", {}).get(t, {})
                buy_p = pos_info.get('buy_p', curr_p)
                pos_info['max_p'] = max(pos_info.get('max_p', buy_p), curr_p)
                state.setdefault("positions", {})[t] = pos_info
                if curr_p < buy_p * 1.01:
                    print(f"  ⏭️ {t}: 손절 위험 (현가 {curr_p:,.0f} < 매입가 {buy_p:,.0f} × 1.01) (패스)")
                    continue
                is_exit, reason = check_pro_exit(curr_p, pos_info, ohlcv)
                if is_exit:
                    # 최대 3회 재시도
                    retry_count = 0
                    max_retries = 3
                    resp = None
                    
                    while retry_count < max_retries:
                        resp = upbit.sell_market_order(t, qty)
                        if resp:
                            break
                        retry_count += 1
                        if retry_count < max_retries:
                            print(f"  ⚠️ {t} 매도 실패 (#{retry_count}): upbit API 오류 → 재시도")
                            import time
                            time.sleep(1)
                    
                    if resp:
                        profit_rate = ((curr_p - buy_p) / buy_p) * 100 if buy_p > 0 else 0.0
                        stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})
                        if profit_rate > 0:
                            stats["wins"] += 1
                        else:
                            stats["losses"] += 1
                        stats["total_profit"] = float(stats.get("total_profit", 0.0) or 0.0) + float(profit_rate)
                        _record_trade_event("COIN", t, "SELL", qty, price=curr_p, profit_rate=profit_rate, reason=reason)
                        print(f"  ✅ [코인 매도 체결] {t} | 수익률: {profit_rate:+.2f}% | 사유: {reason}")
                        send_telegram(f"🚨 [코인 추세종료 매도] {t}\n사유: {reason}\n최종 수익률: {profit_rate:.2f}%")
                        del state["positions"][t]
                        set_cooldown(state, t)
                        save_state(STATE_PATH, state)
                    else:
                        print(f"  ❌ {t} 매도 최종 실패 ({retry_count}회 시도): upbit API 오류")

            if coin_weather == "🌧️ BEAR":
                print("  ⏭️ 코인 시장이 베어 상태라 신규 매수 안함 (패스)")
            else:
                try:
                    markets = [m['market'] for m in requests.get("https://api.upbit.com/v1/market/all", timeout=10).json() if m.get('market', '').startswith("KRW-")]
                    tickers_data = requests.get("https://api.upbit.com/v1/ticker?markets=" + ",".join(markets), timeout=10).json()
                    scan_targets = [x['market'] for x in sorted(tickers_data, key=lambda x: x.get('acc_trade_price_24h', 0), reverse=True)[:20]]
                except Exception:
                    scan_targets = ["KRW-BTC", "KRW-ETH"]

                print(f"  -> 🪙 코인 실시간 수급 상위 {len(scan_targets)}개 정밀 분석 시작!")
                for idx, t in enumerate(scan_targets, 1):
                    if in_cooldown(state, t):
                        print(f"  ⏭️ {t}: 쿨다운 중 (패스)")
                        continue
                    df_upbit = pyupbit.get_ohlcv(t, interval="day", count=250)
                    if df_upbit is None or len(df_upbit) < 20:
                        print(f"  ⏭️ {t}: OHLCV 데이터 부족 (패스)")
                        continue
                    ohlcv = [{'o': row['open'], 'h': row['high'], 'l': row['low'], 'c': row['close'], 'v': row['volume']} for _, row in df_upbit.iterrows()]
                    is_buy, sl_p, s_name = calculate_pro_signals(ohlcv, coin_weather, t, t, idx, len(scan_targets))
                    if not is_buy:
                        continue
                    if not can_open_new(t, state, max_positions=3):
                        print(f"  ⏭️ {t}: 포지션 개수 초과 (3개) (패스)")
                        continue
                    if coin_weather == "☀️ BULL":
                        budget = krw_bal * 0.50
                    elif coin_weather == "☁️ SIDEWAYS":
                        budget = krw_bal * 0.40
                    else:
                        budget = krw_bal * 0.30
                    if budget > 5500:
                        resp = upbit.buy_market_order(t, budget)
                        if resp:
                            current_p = pyupbit.get_current_price(t)
                            coin_qty = budget / current_p if current_p > 0 else 0
                            print(f"  ✅ [코인 매수 체결] {t} | {int(current_p):,}원 × {coin_qty:.4f} | 손절가: {sl_p:,.0f}원")
                            send_telegram(f"🎯 [코인 V5.0 기관매수] {t}\n평단가: {int(current_p):,}원 × {coin_qty:.4f} | 손절가: {sl_p:,.0f}원\n전략: {s_name}")
                            state.setdefault("positions", {})[t] = {'buy_p': current_p, 'sl_p': sl_p, 'max_p': current_p}
                            _record_trade_event("COIN", t, "BUY", budget, price=current_p, profit_rate=None, reason=s_name)
                            set_cooldown(state, t)
                            save_state(STATE_PATH, state)
                            held_coins.append(t)
                            krw_bal -= budget
                    else:
                        print(f"  ⏭️ {t}: 예수금 부족 (현재: {int(krw_bal):,}원 < 필요: 5,500원) (패스)")
    else:
        print("💤 코인은 점검 또는 데이터 조회 불가 상태입니다.")

    save_state(STATE_PATH, state)
    print("="*60)

# =====================================================================
# 7. 스케줄러
# =====================================================================
def run_continuously(interval=1):
    """스케줄러 루프를 백그라운드에서 실행"""
    class ScheduleThread(threading.Thread):
        @classmethod
        def run(cls):
            while True:
                schedule.run_pending()
                time.sleep(interval)
    
    continuous_thread = ScheduleThread()
    continuous_thread.daemon = True
    continuous_thread.start()

# =====================================================================
# 8. 메인 진입점
# =====================================================================
if __name__ == "__main__":
    print("=" * 50)
    print("🤖 V6.5 통합 자동매매 봇 (완전판)")
    print("=" * 50)

    print("[초기화] KIS 토큰 및 브로커 객체 설정...")
    refresh_brokers_if_needed()
    if broker_kr is None:
        print("🚨 브로커 초기화 실패. 프로그램을 종료합니다.")
        sys.exit(1)
    print("[초기화] 완료.")

    # 시작 시 즉시 실행
    held_kr = get_held_stocks_kr()
    held_us = get_held_stocks_us()
    held_coins = get_held_coins()
    
    state = load_state(STATE_PATH)
    sync_all_positions(state, held_kr, held_us, held_coins)
    heartbeat_report()
    run_trading_bot()

    # 스케줄 설정
    schedule.every(4).hours.do(heartbeat_report)
    schedule.every(15).minutes.do(run_trading_bot)
    
    # 스크리너
    schedule.every().day.at("08:45", "Asia/Seoul").do(screener.run_night_screener)
    schedule.every().day.at("22:00", "Asia/Seoul").do(screener.run_night_screener)

    run_continuously()
    print("\n✅ 모든 시스템이 정상적으로 가동되었습니다.")

    while True:
        time.sleep(60)
