# -*- coding: utf-8 -*-
"""
한국투자증권 Open API + ``mojito`` 브로커 래퍼.

책임
    * OAuth 토큰 발급/저장, ``broker_kr``(서울)·``broker_us``(나스닥) 생성.
    * 잔고·시세·주문(시장가/지정가 등) — 세부는 파일 하반부 함수들.

주의
    * ``verify=False`` 가 들어간 호출이 있을 수 있으니(레거시) 운영 환경에서는 증권사 정책에 맞게 조정한다.
    * ``configure(config)`` 가 선행되어야 ``_cfg`` 가 채워진다.
"""
from datetime import datetime, timedelta

import mojito
import requests
import yfinance as yf

from utils.helpers import load_kis_token, save_kis_token
from utils.telegram import send_telegram

_cfg = None
broker_kr = None
broker_us = None
KIS_TOKEN = None

_us_ticker_exchange_cache = {}


def configure(config: dict):
    global _cfg
    _cfg = config
    try:
        from api import coin_config as _coin_cfg

        _coin_cfg.configure(config)
    except Exception:
        pass


def _split_account_no(acc_no: str):
    try:
        raw = (acc_no or "").strip()
        if "-" in raw:
            cano, prdt = raw.split("-", 1)
            return cano.strip(), prdt.strip()
        return raw[:8].strip(), raw[8:].strip()
    except Exception:
        return "", ""


def get_us_ticker_exchange(ticker):
    """yfinance를 사용하여 미국 주식 티커의 거래소를 조회합니다."""
    if ticker in _us_ticker_exchange_cache:
        return _us_ticker_exchange_cache[ticker]

    try:
        stock = yf.Ticker(ticker)
        exchange = stock.info.get('exchange')

        if exchange in ['NMS', 'NAS', 'NASDAQ']:
            _us_ticker_exchange_cache[ticker] = 'NASD'
            return 'NASD'
        elif exchange in ['NYQ', 'NYS', 'NYSE']:
            _us_ticker_exchange_cache[ticker] = 'NYSE'
            return 'NYSE'
        else:
            # 기본값을 NASD로 하고, 다른 거래소는 필요시 추가
            _us_ticker_exchange_cache[ticker] = 'NASD'
            return 'NASD'
    except Exception as e:
        print(f"  ⚠️ [{ticker}] 거래소 조회 실패: {e}")
        # 실패 시 기본값 NASD 반환
        return 'NASD'


def _create_brokers():
    """Mojito 브로커 객체를 (재)생성합니다."""
    global broker_kr, broker_us
    try:
        broker_kr = mojito.KoreaInvestment(
            api_key=_cfg["kis_key"], api_secret=_cfg["kis_secret"],
            acc_no=_cfg["kis_account"], exchange='서울'
        )
        broker_us = mojito.KoreaInvestment(
            api_key=_cfg["kis_key"], api_secret=_cfg["kis_secret"],
            acc_no=_cfg["kis_account"], exchange='나스닥'
        )
        from api import coin_config as _coin_cfg

        _ae = _coin_cfg.active_exchange()
        if _ae == "UPBIT" and _coin_cfg.upbit_enabled():
            from api import upbit_api as _upbit_api

            _upbit_api.init_upbit(_cfg)
        elif _ae == "BINANCE" and _coin_cfg.binance_enabled():
            from api import binance_api as _bn

            _bn.init_binance(_cfg)

        # KIS 토큰 발급 (mojito는 자동 발급 안 함)
        token_data = issue_new_kis_token()
        if token_data and 'access_token' in token_data:
            # Bearer 중복 방지: 이미 "Bearer "가 있으면 제거
            token = token_data['access_token'].replace('Bearer ', '').strip()
            broker_kr.access_token = token
            broker_us.access_token = token
        else:
            print("⚠️ 토큰 발급 실패 - 기존 토큰 사용 시도")
            token_data = load_kis_token()
            if token_data and 'access_token' in token_data:
                token = token_data['access_token'].replace('Bearer ', '').strip()
                broker_kr.access_token = token
                broker_us.access_token = token
    except Exception as e:
        print(f"🚨 브로커 객체 생성 실패: {e}")
        send_telegram(f"🚨 [긴급] 브로커 객체 생성에 실패했습니다. 키/계좌번호 설정을 확인하세요.\n{e}")
        # sys.exit 금지: GUI·adjust_capital 등은 except Exception 으로 처리하고 프로세스 유지.
        raise RuntimeError(f"브로커 객체 생성 실패: {e}") from e


def _create_brokers_safe() -> bool:
    """GUI·adjust_capital 등에서 실패 시 프로세스 종료 없이 False."""
    try:
        _create_brokers()
        return True
    except RuntimeError as e:
        print(f"⚠️ 브로커 생성 실패: {e}")
        return False


def issue_new_kis_token():
    """새로운 토큰을 발급받아 파일에 저장합니다."""
    print("  -> ⏳ 새로운 KIS 토큰 발급을 시도합니다.")
    try:
        # 🔧 직접 API를 호출하여 토큰을 발급받습니다
        auth_url = "https://openapi.koreainvestment.com:9443/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": _cfg["kis_key"],
            "appsecret": _cfg["kis_secret"]
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
        old_token = str(broker_kr.access_token) if broker_kr and hasattr(broker_kr, 'access_token') else "없음"
        if len(old_token) > 20:
            print(f"     [이전 토큰] {old_token[:20]}...")
        else:
            print(f"     [이전 토큰] {old_token}")

        if not _create_brokers_safe():
            return

        new_token = str(broker_kr.access_token) if broker_kr and hasattr(broker_kr, 'access_token') else "없음"
        if len(new_token) > 20:
            print(f"     [새 토큰] {new_token[:20]}...")
        else:
            print(f"     [새 토큰] {new_token}")
        print("  -> ✅ 브로커 재생성 완료")
        return

    # 브로커가 없으면 생성
    if broker_kr is None or broker_us is None:
        if not _create_brokers_safe():
            return
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
        _create_brokers_safe()


def get_us_cash_real(broker):
    """[직통] 미장 예수금 상세 조회 (토큰 재활용)"""
    global KIS_TOKEN
    base_url = getattr(broker, "base_url", "https://openapi.koreainvestment.com:9443")
    is_mock = "vps" in base_url or "vts" in base_url
    if not KIS_TOKEN:
        try:
            auth_url = f"{base_url}/oauth2/tokenP"
            body = {"grant_type": "client_credentials", "appkey": _cfg["kis_key"], "appsecret": _cfg["kis_secret"]}
            res = requests.post(auth_url, json=body)
            KIS_TOKEN = res.json().get("access_token")
        except Exception as e:
            print(f"⚠️ 직통 토큰 발급 실패: {e}")

    try:
        tr_id = "VTTT3007R" if is_mock else "JTTT3007R"
        url = f"{base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
        headers = {
            "content-type": "application/json", "authorization": f"Bearer {KIS_TOKEN}",
            "appkey": _cfg["kis_key"], "appsecret": _cfg["kis_secret"],
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
            if amt == 0.0:
                amt = float(out.get('frcr_ord_psbl_amt1', 0.0))
            return amt
        return 0.0
    except Exception:
        return 0.0


def get_kis_ohlcv(broker, code, timeframe='D', count=60):
    """KIS API로 OHLCV 가져오기"""
    try:
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
    except Exception:
        return []


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
        "custtype": "P",
        "hashkey": ""
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


def get_kis_top_trade_value(broker=None, limit=100):
    """네이버 금융 실시간 거래대금 상위 종목 스캔 (KIS API 한계 극복)"""
    from bs4 import BeautifulSoup
    import re

    url = "https://finance.naver.com/sise/sise_quant_high.naver"
    headers = {'User-Agent': 'Mozilla/5.0'}

    try:
        res = requests.get(url, headers=headers)
        soup = BeautifulSoup(res.text, 'html.parser')

        tickers = []
        for a_tag in soup.select('a.tltle'):
            href = a_tag.get('href', '')
            if isinstance(href, list):
                href = href[0] if href else ''

            code_match = re.search(r'code=(\w+)', str(href))
            if code_match:
                code = code_match.group(1)
                # 순수 숫자로만 이루어진 종목 코드만 허용 (우선주/신주인수권 등 문자 포함 제외)
                if code.isdigit():
                    tickers.append(code)
                    if len(tickers) >= limit:
                        break
        return tickers
    except Exception as e:
        print(f"거래대금 순위 조회 중 오류: {e}")
        return []


def get_kis_market_cap_rank(broker=None, limit=100):
    """네이버 금융 시가총액 상위 종목 조회 (KIS API 한계 극복)"""
    from bs4 import BeautifulSoup
    import re

    headers = {'User-Agent': 'Mozilla/5.0'}
    tickers = []

    try:
        # 네이버 금융 시가총액은 페이지당 50개 항목 제공
        for page in range(1, 5):  # 1~4페이지 (최대 200개)
            url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok=0&page={page}"
            res = requests.get(url, headers=headers)
            soup = BeautifulSoup(res.text, 'html.parser')

            table = soup.find('table', {'class': 'type_2'})
            if table:
                rows = table.find_all('tr')
                for row in rows:
                    cols = row.find_all('td')
                    if len(cols) > 1:
                        a_tag = cols[1].find('a')
                        if a_tag:
                            href = a_tag.get('href', '')
                            if isinstance(href, list):
                                href = href[0] if href else ''

                            code_match = re.search(r'code=(\w+)', str(href))
                            if code_match:
                                code = code_match.group(1)
                                if code.isdigit():
                                    tickers.append(code)
                                    if len(tickers) >= limit:
                                        return tickers
        return tickers
    except Exception as e:
        print(f"시총 순위 가져오기 에러: {e}")
        return []


def execute_us_order_direct(broker, side, ticker, qty, price):
    """[최종 완전판] 한투 미장 직통 주문기 (거래소 자동분류 + 토큰 정제 + Hashkey)"""
    is_mock = "vps" in broker.base_url or "vts" in broker.base_url
    if side == "buy":
        tr_id = "VTTT1002U" if is_mock else "TTTT1002U"
    else:
        tr_id = "VTTT1001U" if is_mock else "TTTT1006U"

    excg_cd = get_us_ticker_exchange(ticker)
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

    # 🚨 [핵심 수리] 한투 서버 렉으로 인한 봇 강제종료 방어막
    hashkey = ""
    try:
        hash_url = f"{broker.base_url}/uapi/hashkey"
        hash_headers = {
            "content-type": "application/json",
            "appkey": broker.api_key.strip(),
            "appsecret": broker.api_secret.strip()
        }
        hash_res = requests.post(hash_url, headers=hash_headers, json=data, timeout=5).json()
        hashkey = hash_res.get("HASH", "")
    except Exception as e:
        print(f"  ⚠️ Hashkey 발급 실패 (무시하고 주문 강행): {e}")

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


def get_balance_with_retry():
    """국내 잔고 조회 (재시도 포함, tr_cont 에러 우회)"""
    try:
        return broker_kr.fetch_balance()
    except KeyError as e:
        if str(e) == "'tr_cont'":
            # mojito 라이브러리의 헤더 버그 우회 - 직접 API 호출
            try:
                access_token = broker_kr.access_token if broker_kr else ''
                cano, prdt_cd = _split_account_no(_cfg.get('kis_account', ''))
                url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/inquire-balance"
                headers = {
                    "content-type": "application/json; charset=utf-8",
                    "authorization": f"Bearer {access_token}",
                    "appkey": _cfg['kis_key'],
                    "appsecret": _cfg['kis_secret'],
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
            except Exception:
                return {}
        return {}
    except Exception:
        return {}


def get_us_positions_with_retry():
    """미국 포지션 조회 (재시도 포함)"""
    try:
        return get_real_us_positions(broker_us)
    except Exception:
        return {}


def get_valid_order_price(price, is_buy=True, is_us=False):
    """
    KIS API 호가 단위에 맞는 주문 가격 계산 (한국거래소 최신 규정 완벽 적용)
    """
    if price <= 0:
        return 0

    # 주문 체결을 위해 매수는 상단, 매도는 하단으로 2% 여유를 둡니다.
    adjusted = price * (1.02 if is_buy else 0.98)

    if is_us:
        return round(adjusted, 2)

    # 국내 주식 호가 단위 세분화 적용
    p = int(adjusted)
    if p < 2000:
        unit = 1
    elif p < 5000:
        unit = 5
    elif p < 20000:
        unit = 10
    elif p < 50000:
        unit = 50
    elif p < 200000:
        unit = 100
    elif p < 500000:
        unit = 500
    else:
        unit = 1000

    # 계산된 가격을 호가 단위에 맞게 절사
    return (p // unit) * unit


def create_market_sell_order_kis(ticker, qty, is_us=False, curr_price=None):
    """
    KIS API를 사용한 진짜 시장가(국장) 및 유사 시장가(미장) 매도 주문
    """
    try:
        cano, prdt_cd = _split_account_no(_cfg.get('kis_account', ''))
        broker = broker_us if is_us else broker_kr
        access_token = broker.access_token if broker else ''
        clean_token = access_token.replace('Bearer ', '').strip() if access_token else ''

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {clean_token}",
            "appkey": _cfg['kis_key'],
            "appsecret": _cfg['kis_secret'],
            "custtype": "P"
        }

        # 🚨 국장/미장 API 엔드포인트 및 파라미터 완벽 분리
        if is_us:
            url = "https://openapi.koreainvestment.com:9443/uapi/overseas-stock/v1/trading/order"
            headers["tr_id"] = "JTTT1006U"  # 미국 매도 실전 TR_ID

            body = {
                "CANO": cano,
                "ACNT_PRDT_CD": prdt_cd,
                "PDNO": ticker,
                "ORD_DVSN": "00",  # 미장은 시장가 불가, 지정가(00) 필수
                "ORD_QTY": str(int(qty)),
                # 💡 미장 매도는 하한가 또는 현재가의 -3%로 설정된 값이 들어와야 즉시 체결됨
                "OVRS_ORD_UNPR": str(get_valid_order_price(curr_price, is_buy=False, is_us=is_us)),
                "EXCG_CD": "NASD",
                "ORD_SVR_DVSN_CD": "0"
            }
        else:
            url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/order-cash"
            headers["tr_id"] = "TTTC0801U"  # 국내 매도 실전 TR_ID

            body = {
                "CANO": cano,
                "ACNT_PRDT_CD": prdt_cd,
                "PDNO": ticker,
                "ORD_DVSN": "01",  # 🚨 국장 완벽한 시장가!
                "ORD_QTY": str(int(qty)),
                "ORD_UNPR": "0",   # 🚨 시장가는 단가를 무조건 0으로 셋팅
                "SLL_TYPE": "01"
            }

        res = requests.post(url, json=body, headers=headers, verify=False)
        resp = res.json()

        return {
            'rt_cd': resp.get('rt_cd', '1'),
            'msg_cd': resp.get('msg_cd', ''),
            'msg1': resp.get('msg1', ''),
            'output': resp.get('output', {})
        }
    except Exception as e:
        print(f"     ❌ KIS 시장가 매도 에러: {e}")
        return {'rt_cd': '1', 'msg_cd': 'ERROR', 'msg1': str(e), 'output': {}}


def create_market_buy_order_kis(ticker, qty, is_us=False, curr_price=None):
    """
    KIS API를 사용한 진짜 시장가(국장) 및 유사 시장가(미장) 매수 주문
    """
    try:
        cano, prdt_cd = _split_account_no(_cfg.get('kis_account', ''))
        broker = broker_us if is_us else broker_kr
        access_token = broker.access_token if broker else ''
        clean_token = access_token.replace('Bearer ', '').strip() if access_token else ''

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {clean_token}",
            "appkey": _cfg['kis_key'],
            "appsecret": _cfg['kis_secret'],
            "custtype": "P"
        }

        # 🚨 국장/미장 API 엔드포인트 및 파라미터 완벽 분리
        if is_us:
            url = "https://openapi.koreainvestment.com:9443/uapi/overseas-stock/v1/trading/order"
            headers["tr_id"] = "JTTT1002U"  # 미국 매수 실전 TR_ID

            body = {
                "CANO": cano,
                "ACNT_PRDT_CD": prdt_cd,
                "PDNO": ticker,
                "ORD_DVSN": "00",  # 미장은 지정가(00) 필수
                "ORD_QTY": str(int(qty)),
                # 💡 미장 매수는 상한가 또는 현재가의 +3%로 설정된 값이 들어와야 즉시 체결됨
                "OVRS_ORD_UNPR": str(get_valid_order_price(curr_price, is_buy=True, is_us=is_us)),
                "EXCG_CD": "NASD",
                "ORD_SVR_DVSN_CD": "0"
            }
        else:
            url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/order-cash"
            headers["tr_id"] = "TTTC0802U"  # 국내 매수 실전 TR_ID

            body = {
                "CANO": cano,
                "ACNT_PRDT_CD": prdt_cd,
                "PDNO": ticker,
                "ORD_DVSN": "01",  # 🚨 국장 완벽한 시장가!
                "ORD_QTY": str(int(qty)),
                "ORD_UNPR": "0"    # 🚨 시장가는 단가를 무조건 0으로 셋팅
            }

        res = requests.post(url, json=body, headers=headers, verify=False)
        resp = res.json()

        return {
            'rt_cd': resp.get('rt_cd', '1'),
            'msg_cd': resp.get('msg_cd', ''),
            'msg1': resp.get('msg1', ''),
            'output': resp.get('output', {})
        }
    except Exception as e:
        print(f"     ❌ KIS 시장가 매수 에러: {e}")
        return {'rt_cd': '1', 'msg_cd': 'ERROR', 'msg1': str(e), 'output': {}}
