# -*- coding: utf-8 -*-
"""
V5 전략 코어 — OHLCV 수집, 프로 시그널, 청산 가격.

역할 분리
    * **데이터** — ``get_ohlcv_yfinance`` / ``get_ohlcv_kis_domestic_daily`` / ``get_ohlcv_upbit`` / ``get_ohlcv_realtime`` 등.
    * **시그널** — ``calculate_pro_signals`` (진입 조건 요약).
    * **청산** — ``check_pro_exit``, ``get_final_exit_price`` (손절·트레일링 등).
"""
import pandas as pd
import numpy as np
import time
import yfinance as yf
import requests
import pyupbit

_NAME_CACHE = {}


def _is_valid_symbol_name(name, ticker):
    text = str(name or "").strip()
    base = str(ticker or "").strip()
    if not text or text == base:
        return False
    if "," in text:
        return False
    upper = text.upper()
    if upper.startswith(f"{base}.KS") or upper.startswith(f"{base}.KQ"):
        return False
    return True

def _resolve_display_name(ticker, name=""):
    if _is_valid_symbol_name(name, ticker):
        return str(name)

    key = str(ticker)
    if key.startswith("KRW-"):
        _NAME_CACHE[key] = key
        return key

    if key in _NAME_CACHE:
        return _NAME_CACHE[key]

    resolved = key
    try:
        if key.isdigit():
            info = yf.Ticker(f"{key}.KS").info
            name_yf = info.get('longName', info.get('shortName'))
            if _is_valid_symbol_name(name_yf, key):
                resolved = str(name_yf)
            else:
                info = yf.Ticker(f"{key}.KQ").info
                name_yf = info.get('longName', info.get('shortName'))
                if _is_valid_symbol_name(name_yf, key):
                    resolved = str(name_yf)
        else:
            info = yf.Ticker(key).info
            name_yf = info.get('longName', info.get('shortName'))
            if _is_valid_symbol_name(name_yf, key):
                resolved = str(name_yf)
    except Exception:
        pass

    _NAME_CACHE[key] = resolved
    return resolved
    
    
# 🧰 3. 코인용 OHLCV (Upbit API - 실시간 타격)
def get_ohlcv_upbit(ticker, interval="day", count=200):
    """Upbit API로 OHLCV(200일) 조회 후, 딕셔너리 리스트로 변환"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval=interval, count=count)
        if df is None or df.empty:
            return []
            
        rows = []
        for index, row in df.iterrows():
            rows.append({
                "o": row["open"],
                "h": row["high"],
                "l": row["low"],
                "c": row["close"],
                "v": row["volume"],
            })
        return rows
    except Exception as e:
        print(f"     🔴 get_ohlcv_upbit({ticker}) → 예외: {type(e).__name__}: {e}")
        return []


# 🧰 1. 200일선 전략용 (야후 파이낸스 - 과거 데이터 길게 뽑기)
def _yf_df_to_ohlcv(df):
    """download 결과 DataFrame → ohlcv dict 리스트."""
    if df is None or df.empty:
        return []
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={"Open": "o", "High": "h", "Low": "l", "Close": "c", "Volume": "v"})
    return df[["o", "h", "l", "c", "v"]].to_dict("records")


def get_ohlcv_yfinance(ticker, max_retries=4):
    """
    yfinance로 OHLCV(1년) 조회 후, 딕셔너리 리스트로 변환.

    야후 쪽 일시 오류(HTTP 401 / Invalid Crumb 등)로 빈 프레임이 오면
    짧은 백오프로 재시도해 매도선·지표가 잘못 잡히는 것을 줄인다.
    """
    is_kr = str(ticker).isdigit()
    y_candidates = [f"{ticker}.KS", f"{ticker}.KQ"] if is_kr else [str(ticker)]

    last_problem = None  # 마지막 시도 상태(예외 객체 또는 "empty")
    for y_ticker in y_candidates:
        for attempt in range(max_retries):
            attempt_exc = None
            try:
                # threads=False: 단일 요청으로 세션·crumb 갱신이 안정적인 편
                df = yf.download(
                    y_ticker,
                    period="1y",
                    interval="1d",
                    progress=False,
                    threads=False,
                )
                ohlcv = _yf_df_to_ohlcv(df)
                if ohlcv:
                    return ohlcv
                last_problem = "empty"
            except Exception as e:
                attempt_exc = e
                last_problem = e

            if attempt < max_retries - 1:
                delay = min(6.0, 0.6 * (2**attempt))
                why = type(attempt_exc).__name__ if attempt_exc else "빈 응답"
                print(
                    f"     ⚠️ [{ticker}] yfinance ({y_ticker}) {attempt + 1}/{max_retries} — {why}, "
                    f"{delay:.1f}s 후 재시도"
                )
                time.sleep(delay)

    if isinstance(last_problem, Exception):
        print(f"     🔴 get_ohlcv_yfinance({ticker}) → {type(last_problem).__name__}: {last_problem}")
    return []

# 🧰 2. 국장 일봉 OHLCV — KIS 전용 (yfinance 호출 없음, 유량 제어는 호출부)
def get_ohlcv_kis_domestic_daily(broker, ticker):
    """국내 6자리 일봉만 KIS ``inquire-daily-itemchartprice`` 로 조회. 실패 시 ``[]``."""
    if not str(ticker).isdigit():
        return []
    try:
        clean_token = str(getattr(broker, "access_token", "") or "").replace("Bearer ", "").strip()
        base_url = getattr(broker, "base_url", "https://openapi.koreainvestment.com:9443")

        tr_id = "FHKST03010100"
        url = f"{base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {clean_token}",
            "appkey": getattr(broker, "api_key", ""),
            "appsecret": getattr(broker, "api_secret", ""),
            "tr_id": tr_id,
            "custtype": "P",
        }
        from datetime import datetime, timedelta

        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=380)).strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": str(ticker),
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "1",
        }

        data = {}
        max_retries = 3
        for attempt in range(max_retries):
            try:
                res = requests.get(url, headers=headers, params=params, timeout=10)
                data = res.json() if res is not None else {}
            except Exception as req_err:
                print(f"     🔴 OHLCV 네트워크 오류 [{attempt+1}/{max_retries}] ({ticker}): {type(req_err).__name__}: {req_err}")
                data = {"rt_cd": "EX", "msg1": f"{type(req_err).__name__}: {req_err}"}

            if str(data.get("rt_cd", "")) == "0":
                break

            msg = str(data.get("msg1", "") or "")
            if attempt < max_retries - 1:
                print(f"     ⚠️  OHLCV 재시도 [{attempt+1}/{max_retries}] ({ticker}): {msg[:50]}")
                time.sleep(0.3 * (attempt + 1))

        if str(data.get("rt_cd", "")) != "0":
            print(f"     🔴 OHLCV 조회 실패 ({ticker}): rt_cd={data.get('rt_cd')}, msg={data.get('msg1', 'N/A')}")
            return []

        output2 = data.get("output2", [])
        if not output2:
            print(f"     ⚠️ KIS OHLCV 비어있음 ({ticker})")
            return []

        rows = []
        for item in reversed(output2):
            try:
                rows.append({
                    "o": float(item.get("open", item.get("stck_oprc", 0))),
                    "h": float(item.get("high", item.get("stck_hgpr", 0))),
                    "l": float(item.get("low", item.get("stck_lwpr", 0))),
                    "c": float(item.get("close", item.get("stck_clpr", 0))),
                    "v": float(item.get("volume", item.get("acml_vol", 0))),
                })
            except (ValueError, TypeError):
                continue
        return rows
    except Exception as e:
        print(f"     🔴 get_ohlcv_kis_domestic_daily({ticker}) → 예외: {type(e).__name__}: {e}")
        return []


# 🧰 2b. 5% 갭상승 등 — 국내는 KIS 후 yfinance, 그 외는 yfinance
def get_ohlcv_realtime(broker, ticker):
    """국내 6자리는 KIS 일봉 우선·부족 시 yfinance, 코인은 업비트, 그 외는 yfinance."""
    try:
        if str(ticker).startswith("KRW-"):
            return get_ohlcv_upbit(ticker)

        if str(ticker).isdigit():
            rows = get_ohlcv_kis_domestic_daily(broker, ticker)
            if rows:
                return rows
            print(f"     ⚠️ KIS 일봉 미확보 ({ticker}) → yfinance 백업")
            return get_ohlcv_yfinance(ticker)

        return get_ohlcv_yfinance(ticker)
    except Exception as e:
        print(f"     🔴 get_ohlcv_realtime({ticker}) → 예외: {type(e).__name__}: {e}")
        return []




def calculate_pro_signals(ohlcv, market_weather, ticker="", name="", idx=0, total=0):
    # 구버전 호출 호환 처리
    if isinstance(name, (int, float)) and isinstance(idx, (int, float)) and (not total or total == 0):
        idx, total = int(name), int(idx)
        name = ""

    progress = f"[{idx}/{total}]" if total > 0 else ""
    display_name = f"{name}({ticker})" if name and name != ticker else ticker

    # 🚨 신규 상장 코인/주식 타격용 (30일 최소)
    if not ohlcv or len(ohlcv) < 30:
        print(f"   🔍 {progress} {display_name} ❌ 패스: 데이터 부족 (30일 미만)")
        return False, 0.0, "데이터 부족"

    df = pd.DataFrame(ohlcv)
    
    # 1. 이평선 및 거래량 계산
    df['ma5'] = df['c'].rolling(5).mean()
    df['ma20'] = df['c'].rolling(20).mean()
    df['ma50'] = df['c'].rolling(50).mean()
    df['ma200'] = df['c'].rolling(200).mean()
    df['v_ma20'] = df['v'].rolling(20).mean()
    
    # 2. RSI 계산 (14일)
    delta = df['c'].diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = -1 * delta.clip(upper=0).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    # 3. MACD 계산 (12, 26, 9)
    df['ema12'] = df['c'].ewm(span=12, adjust=False).mean()
    df['ema26'] = df['c'].ewm(span=26, adjust=False).mean()
    df['macd'] = df['ema12'] - df['ema26']
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    
    # 4. 🎯 ATR 계산 (14일) - 변동성 기반 스윙 손절용
    df['tr0'] = abs(df['h'] - df['l'])
    df['tr1'] = abs(df['h'] - df['c'].shift())
    df['tr2'] = abs(df['l'] - df['c'].shift())
    df['tr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
    df['atr'] = df['tr'].rolling(14).mean()

    today = df.iloc[-1]
    yesterday = df.iloc[-2]
    curr_p = today['c']
    today_atr = today['atr']
    
    # --- [타점 검증 필터] ---

    # 🛡️ 퀀트 필터 1: 양봉 캔들 필터 (음봉 절대 매수 금지)
    if curr_p <= today['o']:
        print(f"   🔍 {progress} {display_name} ❌ 패스: 당일 음봉 (시가 이탈)")
        return False, 0.0, "당일 음봉"

    # 🛡️ 퀀트 필터 2: 동적 윗꼬리 제한 (종목별 일평균 변동폭 ATR의 50% 초과 시 상투로 간주)
    upper_tail_len = today['h'] - curr_p
    upper_tail_ratio = (upper_tail_len / today_atr) if today_atr > 0 else 0
    if upper_tail_len > (today_atr * 0.5):
        print(f"   🔍 {progress} {display_name} ❌ 패스: 악성 윗꼬리 발생 (ATR 대비 50% 초과)")
        return False, 0.0, "동적 윗꼬리 과다"

    # 🛡️ 퀀트 필터 3: 동적 이격도 과열 (20일선 기준 ATR의 3배 이상 폭등 시 추격 금지)
    if pd.notna(today['ma20']) and curr_p > (today['ma20'] + (today_atr * 3.0)):
        print(f"   🔍 {progress} {display_name} ❌ 패스: 단기 과열 (20일선 + 3ATR 초과)")
        return False, 0.0, "동적 이격도 과열"
    
    # 체크 1: 20일선 우상향 지지
    if pd.isna(today['ma20']) or curr_p < today['ma20'] or today['ma20'] <= yesterday['ma20']:
        print(f"   🔍 {progress} {display_name} ❌ 패스: 20일선 하락 또는 이탈")
        return False, 0.0, "20일선 하락/이탈"

    # 체크 2: 장기 추세 필터 (국·미·코인 동일 적용)
    is_golden_trend = True
    if pd.notna(today["ma200"]) and pd.notna(today["ma50"]):
        is_golden_trend = bool(today["ma50"] > today["ma200"])
    elif pd.notna(today["ma50"]):
        is_golden_trend = bool(curr_p > today["ma50"])

    if not is_golden_trend:
        print(f"   🔍 {progress} {display_name} ❌ 패스: 장기 상승 추세 아님 (역배열 방어막 작동)")
        return False, 0.0, "장기추세 미달"

    # 체크 3: 3중 스나이퍼 교차 검증 (수급 + MACD + RSI)
    is_volume_surged = pd.notna(today['v_ma20']) and today['v'] > today['v_ma20']
    is_macd_bullish = pd.notna(today['macd_signal']) and today['macd'] > today['macd_signal']
    is_rsi_healthy = pd.notna(today['rsi']) and (50 <= today['rsi'] <= 75)

    # --- [최종 매수 판단] ---
    if is_volume_surged and is_macd_bullish and is_rsi_healthy:
        strategy_name = "V6 스나이퍼(수급+MACD+RSI+상투방지)"
        
        # 🎯 [변경] 4000억 퀀트 매니저의 실전 방어선 (ATR 1.0배 노이즈 허용 + 최대 10% 하드스탑 방어막)
        calculated_sl = today['ma20'] - (today_atr * 1.0)
        absolute_sl = curr_p * 0.90  # 현재가 대비 -10% 절대 방어선
        
        # 둘 중 '더 높은 가격(손실이 적은 가격)'을 최종 손절선으로 채택!
        stop_loss_price = max(calculated_sl, absolute_sl)
        
        print(f"   🔥 {progress} {display_name} 🎯 3중 교차검증+상투방지 완료! [{strategy_name}]")
        print(f"      └ 세부지표: RSI({today['rsi']:.1f}), 윗꼬리({upper_tail_ratio*100:.1f}%), 이격도 적합")
        return True, stop_loss_price, strategy_name
    else:
        # 🗣️ 왜 패스했는지 속 시원하게 다 불어라!
        reason_str = []
        if not is_volume_surged: reason_str.append("수급부족")
        if not is_macd_bullish: reason_str.append("MACD데드")
        if not is_rsi_healthy: reason_str.append(f"RSI이탈({today['rsi']:.1f})")
        
        print(f"   🔍 {progress} {display_name} ❌ 패스: 보조지표 미달 ({', '.join(reason_str)})")

    return False, 0.0, "보조지표 교차검증 미달"

def _finite_price(x, fallback: float = 0.0) -> float:
    """NaN/inf/비수치는 fallback(그것도 비정상이면 0). 매도선 로그·비교용."""
    try:
        v = float(x)
        if np.isfinite(v):
            return v
    except (TypeError, ValueError):
        pass
    try:
        fb = float(fallback)
        if np.isfinite(fb):
            return fb
    except (TypeError, ValueError):
        pass
    return 0.0


# 🛑 V5.0 기관급 매도 엔진: "샹들리에 트레일링 스탑 (Chandelier Exit)"
def get_chandelier_exit(curr_p, pos_info, ohlcv):
    if not ohlcv:
        return _finite_price(pos_info.get("sl_p"), 0.0)
    fb_sl = _finite_price(pos_info.get("sl_p"), 0.0)
    cp = _finite_price(curr_p, 0.0)
    try:
        df = pd.DataFrame(ohlcv)
        for col in ("h", "l", "c"):
            if col not in df.columns:
                return fb_sl or cp * 0.9 if cp > 0 else 0.0
        df = df.copy()
        df["prev_c"] = df["c"].shift(1)
        df["tr"] = df.apply(
            lambda x: max(
                x["h"] - x["l"],
                abs(x["h"] - x["prev_c"]) if pd.notna(x["prev_c"]) else 0,
                abs(x["l"] - x["prev_c"]) if pd.notna(x["prev_c"]) else 0,
            ),
            axis=1,
        )
        atr = df["tr"].rolling(14, min_periods=1).mean().iloc[-1]
        if pd.isna(atr) or not np.isfinite(float(atr)) or float(atr) <= 0:
            hl = (df["h"] - df["l"]).rolling(14, min_periods=1).mean().iloc[-1]
            atr = hl if pd.notna(hl) and np.isfinite(float(hl)) and float(hl) > 0 else None
        if atr is None or not np.isfinite(float(atr)) or float(atr) <= 0:
            atr = cp * 0.02 if cp > 0 else 1.0
        else:
            atr = float(atr)
        max_price = max(_finite_price(pos_info.get("max_p", cp), cp), cp)
        if max_price <= 0:
            return fb_sl or (cp * 0.9 if cp > 0 else 0.0)
        out = max_price - (atr * 2)
        return _finite_price(out, fb_sl or (cp * 0.9 if cp > 0 else 0.0))
    except Exception:
        return fb_sl or (cp * 0.9 if cp > 0 else 0.0)


def get_final_exit_price(ticker, curr_p, pos_info, ohlcv):
    """현재가 및 수익률, 코어 종목 여부를 종합해 '최종 매도 컷 라인'을 계산"""
    if not ohlcv:
        return _finite_price(pos_info.get("sl_p"), 0.0)

    cp = _finite_price(curr_p, 0.0)
    sl_fb = _finite_price(pos_info.get("sl_p"), cp * 0.9 if cp > 0 else 0.0)

    # 1. 톱니바퀴 락 (오늘 샹들리에와 기존 저장된 손절가 중 무조건 높은 값 선택) — NaN 금지
    raw_chandelier = get_chandelier_exit(curr_p, pos_info, ohlcv)
    locked_chandelier = max(_finite_price(raw_chandelier, sl_fb), sl_fb)
    
    # 2. 🚨 [수정] 수익률 계산을 '현재가'가 아닌 '최고가(max_p)' 기준으로 변경!
    buy_price = _finite_price(pos_info.get("buy_p", cp), cp)
    max_price = max(_finite_price(pos_info.get("max_p", cp), cp), cp)
    max_profit_rate = ((max_price - buy_price) / buy_price * 100) if buy_price > 0 else 0.0

    # 3. 콘크리트 바닥 (이익 보존 락)
    if max_profit_rate >= 30.0:
        profit_floor = buy_price * 1.15
    elif max_profit_rate >= 20.0:
        profit_floor = buy_price * 1.05
    elif max_profit_rate >= 10.0:
        profit_floor = buy_price * 1.005
    else:
        profit_floor = 0

    final_exit_line = max(locked_chandelier, profit_floor)

    return _finite_price(final_exit_line, sl_fb)


def check_pro_exit(ticker, curr_p, pos_info, ohlcv):
    if not ohlcv: return False, ""

    # 1. 기본 정보 세팅 (기존 함수에서 필요한 값들을 가져옴)
    buy_price = pos_info.get('buy_p', curr_p)
    current_price = curr_p
    profit_rate = ((current_price - buy_price) / buy_price * 100) if buy_price > 0 else 0.0

    # `get_final_exit_price` 함수를 사용하여 최종 매도선 계산
    final_stop_loss = get_final_exit_price(ticker, current_price, pos_info, ohlcv)

    # 3. 장부(bot_state) 업데이트 및 매도 실행
    pos_info['sl_p'] = final_stop_loss

    if current_price <= final_stop_loss:
        print(f"🚨 [익절/손절 트리거 발동] {ticker} 매도 실행! (수익 방어 성공)")
        # 사유 텍스트 다변화

        if profit_rate >= 10.0 and final_stop_loss > get_chandelier_exit(current_price, pos_info, ohlcv):
            reason = f"이익 보존 락(Lock) 발동 (+{profit_rate:.1f}% 구간)"
        else:
            reason = "V5.0 샹들리에 라인 붕괴 (추세 종료)"
        return True, reason
        
    return False, ""