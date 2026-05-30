# -*- coding: utf-8 -*-
"""
V5 전략 코어 — OHLCV 수집, 프로 시그널, 청산 가격.

역할 분리
    * **데이터** — ``get_ohlcv_yfinance`` / ``get_ohlcv_kis_domestic_daily`` / ``get_ohlcv_upbit`` / ``get_ohlcv_realtime`` 등.
    * **시그널** — ``calculate_pro_signals`` (V8 진입), ``check_swing_entry`` / ``check_swing_exit`` (SWING_FIB).
    * **청산** — ``check_pro_exit``, ``get_final_exit_price`` (V8, 샹들리에·20MA/ATR), ``get_swing_exit_display_price`` (SWING_FIB 매도선).

스윙 요약은 README.md §8. 진입: ``check_swing_entry`` (60MA·이격·갭·양봉·윗꼬리·RSI≥35·피보).
청산 HALF: 1.5R 스케일아웃. 하드바닥: 피보·구름 + 시간가중 손절(영업 24h 후).
상수: ``SWING_MA60_MAX_EXTENSION_PCT``, ``SWING_GAP_UP_MAX_PCT``, ``SWING_ENTRY_RSI_MIN`` 등.
매도선: ``get_swing_exit_display_price`` (HALF 목표는 ``get_swing_scale_out_target_price`` 1.5R).
"""
import pandas as pd
import numpy as np
import time
import yfinance as yf
import requests
import pyupbit

from utils.math_utils import calculate_hurst_exponent
from utils.ohlcv_store import load_disk_ohlcv, save_disk_ohlcv
from utils.yfinance_guard import yf_call, yf_suppress_stderr, yf_ticker_allowed, yf_ticker_backoff

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
            from api.kr_stock_meta import resolve_kr_company_name

            broker = None
            try:
                from api import kis_api

                broker = kis_api.broker_kr
            except Exception:
                pass
            name_kr = resolve_kr_company_name(key, broker=broker)
            if _is_valid_symbol_name(name_kr, key):
                resolved = str(name_kr)
        else:
            info = yf_call(lambda: yf.Ticker(key).info, label="name", ticker=key)
            if isinstance(info, dict):
                name_yf = info.get("longName", info.get("shortName"))
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


def _stooq_symbol(ticker: str) -> str:
    t = str(ticker).strip().upper()
    if t.isdigit():
        return f"{t.zfill(6)}.kr"
    return f"{t.lower()}.us"


def get_ohlcv_pykrx(ticker: str) -> list:
    """KRX 일봉(pykrx) — 국장 yfinance 401 시 200봉 백업."""
    code = "".join(ch for ch in str(ticker) if ch.isdigit()).zfill(6)
    if not code or code == "000000":
        return []
    try:
        from datetime import datetime, timedelta

        from pykrx import stock

        end = datetime.now().date()
        start = end - timedelta(days=400)
        df = stock.get_market_ohlcv_by_date(
            start.strftime("%Y%m%d"),
            end.strftime("%Y%m%d"),
            code,
        )
        if df is None or df.empty:
            return []
        rows = []
        for _, row in df.iterrows():
            try:
                c = float(row["종가"])
                if c <= 0:
                    continue
                rows.append(
                    {
                        "o": float(row["시가"]),
                        "h": float(row["고가"]),
                        "l": float(row["저가"]),
                        "c": c,
                        "v": float(row["거래량"]),
                    }
                )
            except (TypeError, ValueError, KeyError):
                continue
        if rows:
            print(f"     ✅ [{code}] pykrx 일봉 {len(rows)}봉")
        return rows
    except Exception as e:
        print(f"     ⚠️ [{code}] pykrx 조회 실패: {type(e).__name__}: {e}")
        return []


def get_ohlcv_stooq(ticker: str, *, apikey: str = "") -> list:
    """Stooq 일봉 CSV — config ``stooq_apikey`` 있을 때만 (무료 직접 URL은 키 필요)."""
    sym = _stooq_symbol(ticker)
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    if apikey:
        url = f"{url}&apikey={apikey.strip()}"
    try:
        res = requests.get(
            url,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (compatible; quant-bot/1.0)"},
        )
        if res.status_code != 200 or not (res.text or "").strip():
            return []
        if "get_apikey" in res.text.lower() or "apikey" in res.text.lower()[:200]:
            return []
        import io

        lines = [ln.strip() for ln in res.text.splitlines() if ln.strip()]
        start = 0
        for i, ln in enumerate(lines):
            low = ln.lower()
            if low.startswith("date,") or low.startswith("data,"):
                start = i
                break
        csv_text = "\n".join(lines[start:])
        if not csv_text:
            return []
        df = pd.read_csv(io.StringIO(csv_text))
        if df is None or df.empty:
            return []
        cols = {str(c).strip().lower(): c for c in df.columns}
        close_col = cols.get("close")
        if not close_col:
            return []
        rows = []
        for _, row in df.iterrows():
            try:
                c = float(row[close_col])
                if c <= 0:
                    continue
                def _col(name: str, default: float) -> float:
                    key = cols.get(name)
                    if not key:
                        return default
                    try:
                        v = float(row[key])
                        return v if v > 0 else default
                    except (TypeError, ValueError):
                        return default

                rows.append(
                    {
                        "o": _col("open", c),
                        "h": _col("high", c),
                        "l": _col("low", c),
                        "c": c,
                        "v": _col("volume", 0.0),
                    }
                )
            except (TypeError, ValueError):
                continue
        if len(rows) > 260:
            rows = rows[-260:]
        if rows:
            print(f"     ✅ [{ticker}] Stooq 일봉 {len(rows)}봉 ({sym})")
        return rows
    except Exception as e:
        print(f"     ⚠️ [{ticker}] Stooq 조회 실패: {type(e).__name__}: {e}")
        return []


def get_ohlcv_yfinance(ticker, max_retries=4):
    """
    yfinance로 OHLCV(1년) 조회 후, 딕셔너리 리스트로 변환.

    401 시 해당 티커만 짧게 백오프 — 전역 차단 없음(Stooq·KIS·디스크 캐시 우선).
    """
    if not yf_ticker_allowed(ticker):
        return []

    is_kr = str(ticker).isdigit()
    y_candidates = [f"{ticker}.KS", f"{ticker}.KQ"] if is_kr else [str(ticker)]

    last_problem = None
    for y_ticker in y_candidates:
        for attempt in range(max_retries):
            attempt_exc = None
            try:
                with yf_suppress_stderr() as cap:
                    df = yf.download(
                        y_ticker,
                        period="1y",
                        interval="1d",
                        progress=False,
                        threads=False,
                    )
                if cap.getvalue() and (
                    "401" in cap.getvalue().lower() or "unauthorized" in cap.getvalue().lower()
                ):
                    yf_ticker_backoff(ticker)
                    return []
                ohlcv = _yf_df_to_ohlcv(df)
                if ohlcv:
                    return ohlcv
                last_problem = "empty"
            except Exception as e:
                attempt_exc = e
                last_problem = e
                if "401" in str(e).lower() or "unauthorized" in str(e).lower():
                    yf_ticker_backoff(ticker)
                    return []

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
    """국내 6자리는 KIS 일봉 우선·부족 시 yfinance, 코인은 업비트/바이낸스, 그 외는 yfinance."""
    try:
        if str(ticker).startswith("USDT-"):
            try:
                from api import coin_broker

                return coin_broker.fetch_ohlcv(str(ticker), "day", 200)
            except Exception:
                return []
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
    _v8 = "[V8] "

    # V8: 120MA 필수 — 최소 120봉
    if not ohlcv or len(ohlcv) < 120:
        print(f"   🔍 {_v8}{progress} {display_name} ❌ 패스: 데이터 부족 (120일 미만)")
        return False, 0.0, "120일선 데이터 부족"

    df = pd.DataFrame(ohlcv)
    
    # 1. 이평선 및 거래량 계산
    df['ma5'] = df['c'].rolling(5).mean()
    df['ma20'] = df['c'].rolling(20).mean()
    df['ma50'] = df['c'].rolling(50).mean()
    df['ma120'] = df['c'].rolling(120).mean()
    df['ma200'] = df['c'].rolling(200).mean()
    df['v_ma20'] = df['v'].rolling(20).mean()
    df['v_max20'] = df['v'].rolling(20).max()
    
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

    # --- [Hurst Exponent: 추세 vs 횡보] ---
    close_series = df["c"].dropna().astype(float)
    hurst_window = min(100, int(close_series.size))
    if hurst_window >= 50:
        hurst_prices = close_series.tail(hurst_window).tolist()
        hurst_h = calculate_hurst_exponent(hurst_prices)
        if hurst_h < 0.45:
            hurst_reason = f"강한 횡보/역추세 (H={hurst_h:.3f}<0.45)"
            print(
                f"   🔍 {_v8}{progress} {display_name} ❌ 패스: Hurst 차단 — {hurst_reason}"
            )
            return False, 0.0, hurst_reason
    
    # --- [타점 검증 필터] ---

    # 🛡️ 퀀트 필터 1: 양봉 캔들 필터 (음봉 절대 매수 금지)
    if curr_p <= today['o']:
        print(f"   🔍 {_v8}{progress} {display_name} ❌ 패스: 당일 음봉 (시가 이탈)")
        return False, 0.0, "당일 음봉"

    # 🛡️ 퀀트 필터 2: 동적 윗꼬리 제한 (종목별 일평균 변동폭 ATR의 50% 초과 시 상투로 간주)
    upper_tail_len = today['h'] - curr_p
    upper_tail_ratio = (upper_tail_len / today_atr) if today_atr > 0 else 0
    if upper_tail_len > (today_atr * 0.5):
        print(f"   🔍 {_v8}{progress} {display_name} ❌ 패스: 악성 윗꼬리 발생 (ATR 대비 50% 초과)")
        return False, 0.0, "동적 윗꼬리 과다"

    # 🛡️ 퀀트 필터 3: 동적 이격도 과열 (20일선 기준 ATR의 3배 이상 폭등 시 추격 금지)
    if pd.notna(today['ma20']) and curr_p > (today['ma20'] + (today_atr * 3.0)):
        print(f"   🔍 {_v8}{progress} {display_name} ❌ 패스: 단기 과열 (20일선 + 3ATR 초과)")
        return False, 0.0, "동적 이격도 과열"
    
    # 체크 1: 20일선 우상향 지지
    if pd.isna(today['ma20']) or curr_p < today['ma20'] or today['ma20'] <= yesterday['ma20']:
        print(f"   🔍 {_v8}{progress} {display_name} ❌ 패스: 20일선 하락 또는 이탈")
        return False, 0.0, "20일선 하락/이탈"

    # 체크 1b: 120일선 위 (단기 가짜 반등 차단)
    # 예외: 20일 최고 거래량 갱신(Volume Breakout) 시 바닥권 턴어라운드 진입 허용
    is_volume_breakout_20d = (
        pd.notna(today.get("v_max20"))
        and pd.notna(today.get("v"))
        and float(today["v"]) >= float(today["v_max20"])
    )
    if pd.isna(today["ma120"]) or curr_p <= today["ma120"]:
        if not is_volume_breakout_20d:
            ma120_v = float(today["ma120"]) if pd.notna(today["ma120"]) else 0.0
            print(
                f"   🔍 {_v8}{progress} {display_name} ❌ 패스: 120일선 이탈 "
                f"(종가 {curr_p:,.0f} ≤ 120MA {ma120_v:,.0f})"
            )
            return False, 0.0, "120일선 이탈(가짜 반등)"
        ma120_v = float(today["ma120"]) if pd.notna(today["ma120"]) else 0.0
        print(
            f"   ✅ {_v8}{progress} {display_name} 120MA 예외 통과 "
            f"(종가 {curr_p:,.0f} ≤ 120MA {ma120_v:,.0f}, 거래량 {today['v']:,.0f} ≥ 20일최고 {today['v_max20']:,.0f})"
        )

    # 체크 2: 장기 추세 필터 (국·미·코인 동일 적용)
    is_golden_trend = True
    if pd.notna(today["ma200"]) and pd.notna(today["ma50"]):
        is_golden_trend = bool(today["ma50"] > today["ma200"])
    elif pd.notna(today["ma50"]):
        is_golden_trend = bool(curr_p > today["ma50"])

    if not is_golden_trend:
        print(f"   🔍 {_v8}{progress} {display_name} ❌ 패스: 장기 상승 추세 아님 (역배열 방어막 작동)")
        return False, 0.0, "장기추세 미달"

    # 체크 3: 3중 스나이퍼 교차 검증 (수급 + MACD + RSI)
    is_volume_surged = pd.notna(today['v_ma20']) and today['v'] > today['v_ma20']
    is_macd_bullish = pd.notna(today['macd_signal']) and today['macd'] > today['macd_signal']
    is_rsi_healthy = pd.notna(today['rsi']) and (50 <= today['rsi'] <= 75)

    # --- [최종 매수 판단] ---
    if is_volume_surged and is_macd_bullish and is_rsi_healthy:
        strategy_name = "V6 스나이퍼(수급+MACD+RSI+상투방지)"
        
        # 🎯 V8.0 듀얼 레이어 하드스탑
        # 1) 추세선: ma20 - ATR*1.0
        # 2) 절대 방어선: 현재가 - ATR*2.0 (ATR 이상 시), 실패 시 기존 -10% fallback
        calculated_sl = today['ma20'] - (today_atr * 1.0)
        if pd.notna(today_atr) and float(today_atr) > 0:
            absolute_sl = curr_p - (float(today_atr) * 2.0)
        else:
            absolute_sl = curr_p * 0.90  # fallback: 현재가 대비 -10%
        
        # 둘 중 '더 높은 가격(손실이 적은 가격)'을 최종 손절선으로 채택!
        stop_loss_price = max(calculated_sl, absolute_sl)

        print(f"   🔥 {_v8}{progress} {display_name} 🎯 3중 교차검증+상투방지 완료! [{strategy_name}]")
        print(f"      └ {_v8}세부지표: RSI({today['rsi']:.1f}), 윗꼬리({upper_tail_ratio*100:.1f}%), 이격도 적합")
        return True, stop_loss_price, strategy_name
    else:
        # 🗣️ 왜 패스했는지 속 시원하게 다 불어라!
        reason_str = []
        if not is_volume_surged: reason_str.append("수급부족")
        if not is_macd_bullish: reason_str.append("MACD데드")
        if not is_rsi_healthy: reason_str.append(f"RSI이탈({today['rsi']:.1f})")
        
        print(f"   🔍 {_v8}{progress} {display_name} ❌ 패스: 보조지표 미달 ({', '.join(reason_str)})")

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


def _v8_technical_stop_floor_from_ohlcv(ohlcv, reference_price: float) -> float:
    """V8 기술 매도선 — ``max(20MA−ATR×1, 종가−ATR×2)`` (매수 시그널과 동일 구조)."""
    cp = _finite_price(reference_price, 0.0)
    if not ohlcv or cp <= 0:
        return 0.0
    try:
        df = pd.DataFrame(ohlcv)
        for col in ("h", "l", "c"):
            if col not in df.columns:
                return 0.0
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
        atr_s = df["tr"].rolling(14, min_periods=1).mean()
        atr = float(atr_s.iloc[-1]) if len(atr_s) else 0.0
        if not np.isfinite(atr) or atr <= 0:
            atr = cp * 0.02
        ma20_s = df["c"].rolling(20, min_periods=1).mean()
        ma20 = float(ma20_s.iloc[-1]) if len(ma20_s) else 0.0
        calculated_sl = (ma20 - atr * 1.0) if np.isfinite(ma20) and ma20 > 0 else 0.0
        absolute_sl = cp - (atr * 2.0)
        candidates = [x for x in (calculated_sl, absolute_sl) if np.isfinite(x) and x > 0]
        return max(candidates) if candidates else 0.0
    except Exception:
        return 0.0


def get_final_exit_price(ticker, curr_p, pos_info, ohlcv):
    """V8 최종 매도선 — 샹들리에·20MA/ATR·본절 락(최고수익≥4%) 중 높은 값."""
    if not ohlcv:
        return _finite_price(pos_info.get("sl_p"), 0.0)

    cp = _finite_price(curr_p, 0.0)
    sl_fb = _finite_price(pos_info.get("sl_p"), cp * 0.9 if cp > 0 else 0.0)

    current_atr = _finite_price(pos_info.get("current_atr"), 0.0)
    if current_atr <= 0 and cp > 0:
        current_atr = cp * 0.02
    max_price = max(_finite_price(pos_info.get("max_p", cp), cp), cp)
    raw_chandelier = max_price - (float(current_atr) * 2.5)
    locked_chandelier = max(_finite_price(raw_chandelier, sl_fb), sl_fb)

    buy_price = _finite_price(pos_info.get("buy_p", cp), cp)
    max_price = max(_finite_price(pos_info.get("max_p", cp), cp), cp)
    profit_floor = get_v8_profit_lock_floor(buy_price, max_price)

    candidates = [locked_chandelier]
    technical = _v8_technical_stop_floor_from_ohlcv(ohlcv, cp)
    if technical > 0:
        candidates.append(float(technical))
    if profit_floor > 0:
        candidates.append(float(profit_floor))
    final_exit_line = max(candidates)

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
        buy_f = float(buy_price) if buy_price else 0.0
        max_p = max(
            float(pos_info.get("max_p", current_price) or current_price),
            float(current_price),
        )
        max_profit_rate = _max_profit_rate_pct(buy_f, max_p)
        lock_floor = get_v8_profit_lock_floor(buy_f, max_p)
        chandelier = get_chandelier_exit(current_price, pos_info, ohlcv)
        if (
            lock_floor > 0
            and final_stop_loss >= lock_floor * 0.9999
            and lock_floor >= chandelier
        ):
            reason = (
                f"이익 보존 락(Lock) 발동 (본절+{(BREAKEVEN_LOCK_MULT - 1) * 100:.1f}%, "
                f"최고+{max_profit_rate:.1f}%)"
            )
        else:
            reason = "V5.0 샹들리에 라인 붕괴 (추세 종료)"
        return True, reason
        
    return False, ""


def _normalize_ohlcv_df(df: pd.DataFrame) -> pd.DataFrame:
    """스윙 전용 계산용 컬럼 표준화(OHLCV 소문자)."""
    out = df.copy()
    lower_map = {str(c).lower(): c for c in out.columns}
    rename_map = {}
    for k in ("o", "h", "l", "c", "v"):
        if k in lower_map and lower_map[k] != k:
            rename_map[lower_map[k]] = k
    if rename_map:
        out = out.rename(columns=rename_map)
    return out


def _calc_rsi14(close: pd.Series) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _swing_entry_rsi_at_price(w: pd.DataFrame, price: float) -> float | None:
    """판정 종가(실시간 우선)를 당일 종가에 반영한 RSI(14)."""
    if "c" not in w.columns or len(w) < 15:
        return None
    try:
        px = float(price)
    except (TypeError, ValueError):
        return None
    if px <= 0:
        return None
    closes = pd.to_numeric(w["c"], errors="coerce").astype(float).copy()
    closes.iloc[-1] = px
    rsi_s = _calc_rsi14(closes)
    last = rsi_s.iloc[-1]
    if pd.isna(last) or not np.isfinite(last):
        return None
    return float(last)


# 스윙 진입: 당일 고가 대비 현재가 하락(%)이 이 값 이상이면 윗꼬리 설거지로 거절
SWING_UPPER_WICK_DROP_PCT = 5.0
# 60MA 위 추세 속 눌림목 — 판정가가 60MA 대비 이 비율(%) 초과 이격이면 고점 상투로 거절
SWING_MA60_MAX_EXTENSION_PCT = 30.0
# 전일 종가 대비 당일 시가 갭 상승(%) 상한 — 초과 시 뇌동 추격으로 거절
SWING_GAP_UP_MAX_PCT = 3.0
# 진입 시 RSI(14) 하한 — 판정 종가(실시간 우선) 기준, 미만이면 칼날·모멘텀 둔화로 거절
SWING_ENTRY_RSI_MIN = 35.0
# 스윙 손절 피보: 60봉 고저 되돌림 비율 (현재가 **아래** 지지만 허용)
_SWING_FIB_RETRACE_RATIOS = (0.382, 0.500, 0.618)
# 피보·구름 합산이 평단 위로 나오면 롱 손절·매도선을 평단 대비 -3%로 고정 (KR/US/COIN 공통)
SWING_STOP_ABOVE_ENTRY_FALLBACK_MULT = 0.97


def _swing_reference_close(reference_close: float | None, close_bar: float) -> tuple[float, bool]:
    """(판정 종가, 실시간가 사용 여부)."""
    try:
        live = float(reference_close) if reference_close is not None else 0.0
    except (TypeError, ValueError):
        live = 0.0
    if live > 0:
        return live, True
    return float(close_bar), False


def _pick_swing_fib_support_below(price: float, hi60: float, lo60: float) -> tuple[float | None, str]:
    """
    현재가보다 낮은 피보나치 되돌림선 중 가장 가까운(가장 높은) 지지가를 선택.
    38.2·50·61.8% 모두 현재가 이상이면 None.
    """
    if not np.isfinite(hi60) or not np.isfinite(lo60) or hi60 <= lo60:
        return None, "60봉 고저 스팬 무효"
    span = hi60 - lo60
    if span <= 0:
        return None, "60봉 고저 스팬 무효"
    px = float(price)
    below: list[tuple[float, float]] = []
    for ratio in _SWING_FIB_RETRACE_RATIOS:
        level = hi60 - (span * float(ratio))
        if level < px:
            below.append((float(ratio), float(level)))
    if not below:
        return None, (
            f"피보 지지 없음(38.2·50·61.8% 모두 현재가 {px:,.0f} 이상 — 진입 포기)"
        )
    _ratio, chosen = max(below, key=lambda x: x[1])
    return chosen, ""


def _swing_volume_dryup_ok(w: pd.DataFrame, today: pd.Series) -> tuple[bool, str]:
    """당일 거래량 < 5일 평균 (눌림목 Volume Dry-up)."""
    if "v" not in w.columns:
        return False, "거래량(v) 컬럼 부족"
    try:
        vol_today = float(today.get("v", 0) or 0)
    except (TypeError, ValueError):
        vol_today = 0.0
    if vol_today <= 0:
        return False, "당일 거래량 0"
    if len(w) < 5:
        return False, "5일 거래량 평균 산출 불가"
    vol_ma5 = float(w["v"].tail(5).mean())
    if not np.isfinite(vol_ma5) or vol_ma5 <= 0:
        return False, "5일 거래량 평균 무효"
    if vol_today < vol_ma5:
        return True, ""
    return (
        False,
        f"거래량 과다(당일 {vol_today:,.0f} ≥ 5일평균 {vol_ma5:,.0f}) — "
        "거래량 터지며 60MA 눌림 거절",
    )


def check_swing_entry(
    df: pd.DataFrame,
    *,
    reference_close: float | None = None,
) -> tuple[bool, float, str]:
    """
    추세 속 눌림목(Pullback) 스윙 매수 — KR/US/COIN 공통 (HTS 없이 코드 단 검증).

    진입 조건:
        1. 60MA 위 + 60MA 대비 이격 ≤ ``SWING_MA60_MAX_EXTENSION_PCT`` (기본 30%)
        2. 당일 양봉 (시가 < 판정가) — ``reference_close`` 우선
        3. 전일 종가→당일 시가 갭 < ``SWING_GAP_UP_MAX_PCT`` (기본 3%)
        4. 거래량 Dry-up: 당일 < 5일 평균 거래량
        5. 윗꼬리 < ``SWING_UPPER_WICK_DROP_PCT``
        6. RSI(14) ≥ ``SWING_ENTRY_RSI_MIN`` (판정 종가 기준, 모멘텀 둔화·칼날 방지)
        7. 피보: 60봉 38.2/50/61.8% 중 현재가 **아래** 지지

    ``reference_close`` — 호출 시점 실시간가(KIS·거래소). 없으면 당일 일봉 종가.

    Returns:
        (진입여부, entry_fib_level/sl_p, 실패 시 사유 — 성공 시 "")
    """
    if df is None or len(df) < 60:
        return False, 0.0, "봉 부족(60 미만)"

    w = _normalize_ohlcv_df(df)
    need_cols = {"o", "h", "l", "c", "v"}
    if not need_cols.issubset(set(w.columns)):
        return False, 0.0, "OHLCV 컬럼 부족"

    w["ma60"] = w["c"].rolling(60).mean()

    today = w.iloc[-1]
    close_bar = float(today["c"])
    open_today = float(today["o"])
    high_today = float(today["h"])
    if open_today <= 0:
        return False, 0.0, "당일 시가 무효"

    close_for_candle, used_live = _swing_reference_close(reference_close, close_bar)
    if close_for_candle <= 0:
        return False, 0.0, "판정 종가 무효"

    # 갭 상승 억제 (전일 종가 → 당일 시가)
    if len(w) >= 2:
        prev_close = float(w.iloc[-2]["c"])
        if prev_close > 0:
            gap_pct = (open_today - prev_close) / prev_close * 100.0
            if gap_pct >= SWING_GAP_UP_MAX_PCT:
                return (
                    False,
                    0.0,
                    f"갭상승 과다(전일 종가 {prev_close:,.0f}→시가 {open_today:,.0f}, "
                    f"+{gap_pct:.2f}%≥{SWING_GAP_UP_MAX_PCT:.1f}%)",
                )

    ma60 = float(today["ma60"]) if pd.notna(today["ma60"]) else 0.0
    ma_cap = ma60 * (1.0 + SWING_MA60_MAX_EXTENSION_PCT / 100.0) if ma60 > 0 else 0.0

    # 1. 60일선 위 + 이격 상한 (에베레스트 컷)
    cond_ma = ma60 > 0 and close_for_candle > ma60
    cond_ma_ext_ok = ma_cap <= 0 or close_for_candle <= ma_cap
    # 2. 실시간 양봉 (시가 < 판정가)
    cond_bull = close_for_candle > open_today
    # 3. 거래량 Dry-up (당일 < 5일 평균)
    cond_vol, vol_why = _swing_volume_dryup_ok(w, today)
    # 4. 모멘텀 둔화: 판정 종가 기준 RSI(14) ≥ 35 (과매도 칼날 구간 차단)
    rsi14 = _swing_entry_rsi_at_price(w, close_for_candle)
    cond_rsi_ok = rsi14 is not None and rsi14 >= SWING_ENTRY_RSI_MIN

    recent60 = w.iloc[-60:]
    hi60 = float(recent60["h"].max())
    lo60 = float(recent60["l"].min())

    # 3. 윗꼬리 설거지: 당일 고가 대비 현재가 5% 이상 밀림
    high_for_wick = max(high_today, close_for_candle)
    if high_for_wick > 0:
        wick_drop_pct = (high_for_wick - close_for_candle) / high_for_wick * 100.0
        if wick_drop_pct >= SWING_UPPER_WICK_DROP_PCT:
            src = "실시간" if used_live else "일봉"
            return (
                False,
                0.0,
                f"윗꼬리 설거지({src} 고가 {high_for_wick:,.0f} 대비 -{wick_drop_pct:.2f}%≥{SWING_UPPER_WICK_DROP_PCT:.0f}%)",
            )

    miss: list[str] = []
    if not cond_ma:
        miss.append("종가≤60MA")
    elif not cond_ma_ext_ok:
        ext_pct = (close_for_candle / ma60 - 1.0) * 100.0 if ma60 > 0 else 0.0
        miss.append(
            f"60MA 이격 과다(+{ext_pct:.1f}%>{SWING_MA60_MAX_EXTENSION_PCT:.0f}%, "
            f"판정가 {close_for_candle:,.0f} / 60MA {ma60:,.0f})"
        )
    if not cond_bull:
        pct = ((close_for_candle - open_today) / open_today) * 100.0
        src = "실시간" if used_live else "일봉"
        miss.append(f"당일 음봉({src} 시가 {open_today:,.0f}≥종가 {close_for_candle:,.0f}, {pct:+.2f}%)")
    if not cond_vol:
        miss.append(vol_why)
    if not cond_rsi_ok:
        src = "실시간" if used_live else "일봉"
        if rsi14 is None:
            miss.append("RSI(14) 산출 불가")
        else:
            miss.append(
                f"모멘텀 둔화·칼날({src} RSI {rsi14:.1f}<{SWING_ENTRY_RSI_MIN:.0f}, "
                f"종가 {close_for_candle:,.0f})"
            )
    if miss:
        return False, 0.0, " · ".join(miss)

    fib_stop, fib_why = _pick_swing_fib_support_below(close_for_candle, hi60, lo60)
    if fib_stop is None:
        return False, 0.0, fib_why

    return True, float(fib_stop), ""


# 1차 익절(HALF): 초기 1R 대비 R-Multiple — ``entry_initial_risk_1r`` 기준
SWING_SCALE_OUT_R_MULT = 1.5
# 시간 가중 손절: 영업시간 24h 경과 후 24h마다 (진입가−바닥) 갭의 이 비율만큼 바닥 상향
SWING_TIME_DECAY_START_TRADING_HOURS = 24.0
SWING_TIME_DECAY_GAP_CLOSE_PER_24H = 0.40
# RSI FULL: 이 수익 구간에서만 전량 청산 (+10%↑ 는 수익 락 트레일링에만 맡김)
SWING_RSI_FULL_MIN_PROFIT_PCT = 1.0
SWING_RSI_FULL_MAX_PROFIT_PCT = 10.0
# 본절·수수료 방어선 (V8·스윙 공통) — 활성화 시 평단 × 이 배수
BREAKEVEN_LOCK_MULT = 1.005
# 스윙 수익 락: max_p 기준 최고 수익률이 이 값(%) **초과** 시에만 본절 락 활성화
SWING_PROFIT_LOCK_ACTIVATE_PCT = 3.0
# V8 수익 락: max_p 기준 최고 수익률이 이 값(%) **이상** 도달 시 본절 락 활성화
V8_PROFIT_LOCK_ACTIVATE_PCT = 4.0


def _max_profit_rate_pct(buy_p: float, max_p: float) -> float:
    """max_p 기준 최고 수익률(%)."""
    bp = float(buy_p)
    mp = float(max_p)
    if bp <= 0 or mp <= 0:
        return 0.0
    return (mp - bp) / bp * 100.0


def get_v8_profit_lock_floor(buy_p: float, max_p: float) -> float:
    """V8 본절 락 — 최고 수익 ≥ ``V8_PROFIT_LOCK_ACTIVATE_PCT`` 일 때만 ``BREAKEVEN_LOCK_MULT``."""
    bp = float(buy_p)
    if bp <= 0:
        return 0.0
    if _max_profit_rate_pct(bp, max_p) >= V8_PROFIT_LOCK_ACTIVATE_PCT:
        return bp * BREAKEVEN_LOCK_MULT
    return 0.0


def _append_swing_indicators(w: pd.DataFrame) -> pd.DataFrame:
    """스윙 청산·매도선용 일봉 지표(볼밴·일목·RSI)."""
    out = _normalize_ohlcv_df(w)
    out["rsi14"] = _calc_rsi14(out["c"])
    bb_mid = out["c"].rolling(20).mean()
    bb_std = out["c"].rolling(20).std()
    out["bb_upper"] = bb_mid + (bb_std * 2.0)
    tenkan = (out["h"].rolling(9).max() + out["l"].rolling(9).min()) / 2.0
    kijun = (out["h"].rolling(26).max() + out["l"].rolling(26).min()) / 2.0
    out["senkou_a"] = ((tenkan + kijun) / 2.0).shift(26)
    out["senkou_b"] = ((out["h"].rolling(52).max() + out["l"].rolling(52).min()) / 2.0).shift(26)
    return out


def _swing_cloud_floor_from_row(today: pd.Series) -> float | None:
    if pd.notna(today.get("senkou_a")) and pd.notna(today.get("senkou_b")):
        return float(min(float(today["senkou_a"]), float(today["senkou_b"])))
    return None


def infer_swing_entry_fib_from_ohlcv(ohlcv, reference_price: float) -> float:
    """매수 시점 평단·현재가 기준 60봉 피보 지지(진입 로직과 동일). ``entry_fib_level`` 백필용."""
    try:
        px = float(reference_price)
    except (TypeError, ValueError):
        return 0.0
    if px <= 0 or not ohlcv or len(ohlcv) < 60:
        return 0.0
    w = _normalize_ohlcv_df(pd.DataFrame(ohlcv))
    if not {"h", "l"}.issubset(set(w.columns)):
        return 0.0
    recent60 = w.iloc[-60:]
    hi60 = float(recent60["h"].max())
    lo60 = float(recent60["l"].min())
    fib, _ = _pick_swing_fib_support_below(px, hi60, lo60)
    return float(fib) if fib is not None and fib > 0 else 0.0


def reconcile_swing_position(
    pos_info: dict,
    ohlcv,
    *,
    reference_price: float | None = None,
) -> bool:
    """
    SWING_FIB 장부 보정 — ``strategy_type``·``entry_fib_level`` 누락 시 복구.

    ``sl_p``(통합 매도선)를 피보 대용으로 쓰지 않음(순환·왜곡 방지).
    """
    if not isinstance(pos_info, dict):
        return False
    changed = False
    st = str(pos_info.get("strategy_type") or "").strip().upper()
    tier = str(pos_info.get("tier") or "").strip().upper()
    if st != "SWING_FIB" and tier in ("SWING_FIB", "SWING"):
        pos_info["strategy_type"] = "SWING_FIB"
        changed = True
    if str(pos_info.get("strategy_type") or "").upper() != "SWING_FIB":
        return changed
    fib = float(pos_info.get("entry_fib_level", 0.0) or 0.0)
    if fib <= 0 and ohlcv and len(ohlcv) >= 60:
        anchor = _finite_price(reference_price, 0.0)
        if anchor <= 0:
            anchor = _swing_avg_price(pos_info)
        if anchor <= 0:
            try:
                last = ohlcv[-1]
                anchor = float(last.get("c", 0) if isinstance(last, dict) else last["c"])
            except (TypeError, ValueError, KeyError, IndexError):
                anchor = 0.0
        inferred = infer_swing_entry_fib_from_ohlcv(ohlcv, anchor)
        if inferred > 0:
            pos_info["entry_fib_level"] = float(inferred)
            changed = True
    return changed


def _infer_swing_trading_hours_held(
    pos_info: dict,
    market: str | None,
    ticker: str | None,
    trading_hours_held: float | None,
) -> float | None:
    """장부 매수 시각 → 타임스탑과 동일한 영업 보유시간(h). 호출부 미전달 시 자동 산출."""
    if trading_hours_held is not None:
        try:
            th = float(trading_hours_held)
            return th if th >= 0 else None
        except (TypeError, ValueError):
            pass
    p = pos_info if isinstance(pos_info, dict) else {}
    m = _resolve_swing_cap_market(market, ticker)
    try:
        import run_bot as rb

        _, _, th, _ = rb._compute_holding_time_info(p, m)
        return float(th) if th >= 0 else None
    except Exception:
        return None


def _resolve_swing_cap_market(market: str | None, ticker: str | None) -> str:
    """시장 구분(KR / US / COIN) — 가격 라벨·손절 시장 판정."""
    m = str(market or "").strip().upper()
    if m in ("KR", "US", "COIN"):
        return m
    t = str(ticker or "").strip().upper()
    if t.startswith(("KRW-", "USDT-", "BTC-", "ETH-")):
        return "COIN"
    if t.isdigit():
        return "KR"
    if t:
        return "US"
    return "KR"


def _swing_ohlcv_working_df(ohlcv) -> pd.DataFrame | None:
    if ohlcv is None:
        return None
    try:
        if isinstance(ohlcv, pd.DataFrame):
            w = ohlcv if len(ohlcv) >= 60 else None
        else:
            w = pd.DataFrame(ohlcv) if ohlcv and len(ohlcv) >= 60 else None
        if w is not None and len(w) >= 60:
            return _append_swing_indicators(w)
    except Exception:
        return None
    return None


def _swing_clamp_stop_floor_below_entry(buy: float, floor: float) -> float:
    """롱 포지션: 손절·매도선이 평단 위면 평단×``SWING_STOP_ABOVE_ENTRY_FALLBACK_MULT``(-3%)."""
    bp = float(buy)
    fl = float(floor)
    if bp > 0 and fl > bp:
        return bp * SWING_STOP_ABOVE_ENTRY_FALLBACK_MULT
    return fl


def swing_entry_sl_p(buy_p: float, raw_sl: float) -> float:
    """매수 체결 시 장부 ``sl_p`` — 평단 위 후보는 -3%로 보정 (KR/US/COIN)."""
    return _swing_clamp_stop_floor_below_entry(float(buy_p), float(raw_sl))


def _format_swing_price_label(
    px: float,
    *,
    market: str | None = None,
    ticker: str | None = None,
) -> str:
    """스윙 매도 로그·사유용 가격 문자열 (저가 코인 소수 지원)."""
    v = float(px)
    m = _resolve_swing_cap_market(market, ticker)
    if m == "COIN":
        if 0 < v < 1.0:
            s = f"{v:.6f}".rstrip("0").rstrip(".")
            return s if s else "0"
        if v < 100.0:
            return f"{v:.4f}"
        return f"{v:,.0f}"
    if m == "US":
        return f"${v:.2f}"
    return f"{v:,.0f}"


def _swing_base_hard_stop_floor(
    pos_info: dict,
    ohlcv,
    *,
    market: str | None = None,
    ticker: str | None = None,
) -> float:
    """피보·구름 기술 바닥(시간가중·클램프 전)."""
    p = pos_info if isinstance(pos_info, dict) else {}
    buy = _swing_avg_price(p)
    entry_fib = float(p.get("entry_fib_level", 0.0) or 0.0)
    if entry_fib <= 0 and buy > 0:
        entry_fib = infer_swing_entry_fib_from_ohlcv(ohlcv, buy)
    cloud_floor = None
    w = _swing_ohlcv_working_df(ohlcv)
    if w is not None:
        try:
            cloud_floor = _swing_cloud_floor_from_row(w.iloc[-1])
        except Exception:
            cloud_floor = None
    floors: list[float] = []
    if entry_fib > 0 and np.isfinite(entry_fib):
        floors.append(float(entry_fib))
    if cloud_floor is not None and np.isfinite(cloud_floor) and cloud_floor > 0:
        floors.append(float(cloud_floor))
    technical = max(floors) if floors else 0.0
    if technical <= 0:
        return 0.0
    return float(technical)


def _apply_swing_time_decaying_stop(
    buy_p: float,
    base_floor: float,
    trading_hours_held: float,
) -> float:
    """영업 24h 이후 24h마다 손절 바닥을 평단 쪽으로 조임."""
    buy = float(buy_p)
    base = float(base_floor)
    th = float(trading_hours_held)
    if buy <= 0 or base <= 0 or th < SWING_TIME_DECAY_START_TRADING_HOURS:
        return base
    gap = max(0.0, buy - base)
    if gap <= 0:
        return base
    extra_h = th - SWING_TIME_DECAY_START_TRADING_HOURS
    steps = extra_h / 24.0
    lifted = base + gap * min(1.0, steps * SWING_TIME_DECAY_GAP_CLOSE_PER_24H)
    return min(lifted, buy * 0.999)


def get_swing_hard_stop_floor(
    pos_info: dict,
    ohlcv,
    *,
    market: str | None = None,
    ticker: str | None = None,
    trading_hours_held: float | None = None,
) -> float:
    """
    스윙 하드 바닥 — ``max(피보, 구름)`` + 시간 가중 상향(영업시간 기준).

    ``trading_hours_held`` 가 있으면 24h 영업 경과 후 바닥을 평단 방향으로 조임.
    피보·구름이 평단 위이면 **평단 -3%** 로 대체.
    """
    p = pos_info if isinstance(pos_info, dict) else {}
    buy = _swing_avg_price(p)
    base = _swing_base_hard_stop_floor(p, ohlcv, market=market, ticker=ticker)
    if base <= 0:
        return 0.0
    floor = base
    th = _infer_swing_trading_hours_held(
        p, market, ticker, trading_hours_held
    )
    if th is not None and th > 0:
        floor = _apply_swing_time_decaying_stop(buy, base, float(th))
    return _swing_clamp_stop_floor_below_entry(buy, float(floor))


def resolve_swing_initial_1r(
    pos_info: dict,
    ohlcv,
    *,
    market: str | None = None,
    ticker: str | None = None,
) -> tuple[float, float]:
    """
    (``entry_initial_risk_1r``, ``entry_initial_hard_floor``).

    1R = 평단 − 진입 시점 기술 하드 바닥(피보·구름, 시간가중 전).
    """
    p = pos_info if isinstance(pos_info, dict) else {}
    buy = _swing_avg_price(p)
    if buy <= 0:
        return 0.0, 0.0
    stored_r = float(p.get("entry_initial_risk_1r", 0.0) or 0.0)
    stored_h = float(p.get("entry_initial_hard_floor", 0.0) or 0.0)
    if stored_r > 0 and stored_h > 0:
        return stored_r, stored_h
    hard = get_swing_hard_stop_floor(
        p, ohlcv, market=market, ticker=ticker, trading_hours_held=0.0
    )
    if hard <= 0 or hard >= buy:
        return 0.0, hard
    return float(buy - hard), float(hard)


def register_swing_entry_risk_fields(
    pos_info: dict,
    buy_p: float,
    ohlcv,
    *,
    market: str | None = None,
    ticker: str | None = None,
) -> None:
    """매수 체결 직후 장부에 1R·초기 하드 바닥 기록."""
    if not isinstance(pos_info, dict):
        return
    one_r, hard = resolve_swing_initial_1r(
        pos_info, ohlcv, market=market, ticker=ticker
    )
    if hard > 0:
        pos_info["entry_initial_hard_floor"] = float(hard)
    if one_r > 0:
        pos_info["entry_initial_risk_1r"] = float(one_r)


def get_swing_profit_lock_floor(buy_p: float, max_p: float) -> float:
    """
    스윙 본절 락 — max_p 기준 최고 수익률이 ``SWING_PROFIT_LOCK_ACTIVATE_PCT`` **초과**일 때만
    ``평단 × BREAKEVEN_LOCK_MULT``. 그 미만이면 0(피보·구름 하드 바닥만 유지).
    """
    bp = float(buy_p)
    if bp <= 0:
        return 0.0
    if _max_profit_rate_pct(bp, max_p) > SWING_PROFIT_LOCK_ACTIVATE_PCT:
        return bp * BREAKEVEN_LOCK_MULT
    return 0.0


def get_swing_scale_out_target_price(pos_info: dict) -> float | None:
    """1.5R 1차 익절 목표가. ``scale_out_done`` 이면 None."""
    if bool((pos_info or {}).get("scale_out_done", False)):
        return None
    buy = _swing_avg_price(pos_info if isinstance(pos_info, dict) else {})
    one_r = float((pos_info or {}).get("entry_initial_risk_1r", 0.0) or 0.0)
    if buy <= 0 or one_r <= 0:
        return None
    return float(buy + SWING_SCALE_OUT_R_MULT * one_r)


def get_swing_exit_display_price(
    curr_p,
    pos_info,
    ohlcv,
    *,
    market: str | None = None,
    ticker: str | None = None,
    trading_hours_held: float | None = None,
) -> float:
    """
    SWING_FIB **표시용 통합 매도선** (GUI·``sl_p``·로그).

    - 하드스탑(피보·구름) + 본절 락(최고수익>3% 시 평단×1.005) 중 높은 값
    - **실행:** 하드스탑 FULL은 ``check_swing_exit`` 만, 수익 락 이탈은
      ``check_swing_profit_lock_trailing_exit`` 만 담당 (중복 방지).
    """
    p = pos_info if isinstance(pos_info, dict) else {}
    cp = _finite_price(curr_p, 0.0)
    buy = _swing_avg_price(p)
    max_p = max(_finite_price(p.get("max_p", buy), buy), cp)
    hard = get_swing_hard_stop_floor(
        p, ohlcv, market=market, ticker=ticker, trading_hours_held=trading_hours_held
    )
    profit_floor = get_swing_profit_lock_floor(buy, max_p)
    if profit_floor > 0:
        # 수익 락이 평단 위면 예전과 동일(표시·트레일링 — 평단 -3% 클램프 미적용)
        return _finite_price(max(hard, profit_floor), hard)
    return _finite_price(hard, 0.0)


def _swing_avg_price(pos_info: dict) -> float:
    """장부 평단. ``avg_price`` 우선, 없으면 ``buy_p``."""
    p = pos_info if isinstance(pos_info, dict) else {}
    for key in ("avg_price", "buy_p"):
        try:
            v = float(p.get(key, 0) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            return v
    return 0.0


def _swing_current_price(close_today: float, reference_price: float | None) -> float:
    """판정 현재가 — 실시간가가 있으면 우선, 없으면 당일 종가(일봉)."""
    try:
        ref = float(reference_price) if reference_price is not None else 0.0
    except (TypeError, ValueError):
        ref = 0.0
    if ref > 0:
        return ref
    return float(close_today)


def _swing_profit_rate_pct(avg_price: float, current_px: float) -> float:
    if avg_price <= 0 or current_px <= 0:
        return 0.0
    return (current_px - avg_price) / avg_price * 100.0


def check_swing_exit(
    pos_info: dict,
    df: pd.DataFrame,
    *,
    reference_price: float | None = None,
    market: str | None = None,
    ticker: str | None = None,
    trading_hours_held: float | None = None,
) -> tuple[str, str]:
    """
    스윙 매도 타점 판단.

    ``reference_price`` — 장중 실시간가(KIS 등). 없으면 당일 종가로 판정.
    HALF: 현재가 − 평단 ≥ ``SWING_SCALE_OUT_R_MULT`` × ``entry_initial_risk_1r`` (1.5R).
    FULL 하드스탑: 피보·구름 + 시간가중 바닥.
    RSI FULL: 수익 +1%~+10% 구간에서만.

    Returns:
        ("FULL"|"HALF"|"HOLD", 사유)
    """
    if df is None or len(df) < 60:
        return "HOLD", ""

    w = _append_swing_indicators(df)
    need_cols = {"h", "l", "c"}
    if not need_cols.issubset(set(w.columns)):
        return "HOLD", ""

    today = w.iloc[-1]
    prev = w.iloc[-2]

    scale_out = bool((pos_info or {}).get("scale_out_done", False))
    close_today = float(today["c"])
    current_px = _swing_current_price(close_today, reference_price)
    avg_price = _swing_avg_price(pos_info)

    # 1) 하드스탑 — 피보·구름 + 시간가중
    hard_floor = get_swing_hard_stop_floor(
        pos_info,
        w,
        market=market,
        ticker=ticker,
        trading_hours_held=trading_hours_held,
    )
    if hard_floor > 0 and current_px < float(hard_floor):
        cur_lbl = _format_swing_price_label(current_px, market=market, ticker=ticker)
        floor_lbl = _format_swing_price_label(hard_floor, market=market, ticker=ticker)
        return (
            "FULL",
            f"스윙 하드스탑 이탈 (현재가: {cur_lbl} < 기준 {floor_lbl})",
        )

    # 2) HALF — 1.5R 스케일아웃
    if not scale_out and avg_price > 0:
        one_r, _hard0 = resolve_swing_initial_1r(
            pos_info, w, market=market, ticker=ticker
        )
        if one_r > 0:
            target_px = avg_price + SWING_SCALE_OUT_R_MULT * one_r
            profit_amt = current_px - avg_price
            if profit_amt >= SWING_SCALE_OUT_R_MULT * one_r:
                r_now = profit_amt / one_r
                profit_pct = _swing_profit_rate_pct(avg_price, current_px)
                tgt_lbl = _format_swing_price_label(
                    target_px, market=market, ticker=ticker
                )
                return (
                    "HALF",
                    f"{SWING_SCALE_OUT_R_MULT:.1f}R 1차 익절 (현재 {r_now:.2f}R≥{SWING_SCALE_OUT_R_MULT:.1f}R, "
                    f"목표≥{tgt_lbl}, 평단 {avg_price:,.0f} 수익 {profit_pct:+.2f}%)",
                )

    # 3) RSI 데드크로스 FULL — +1% 이상 ~ +10% 미만만 (고수익은 수익 락 트레일링)
    profit_pct = _swing_profit_rate_pct(avg_price, current_px) if avg_price > 0 else 0.0
    if (
        avg_price > 0
        and SWING_RSI_FULL_MIN_PROFIT_PCT <= profit_pct < SWING_RSI_FULL_MAX_PROFIT_PCT
        and pd.notna(prev["rsi14"])
        and pd.notna(today["rsi14"])
        and float(prev["rsi14"]) > 70.0
        and float(today["rsi14"]) < 70.0
    ):
        return (
            "FULL",
            f"RSI 과매수 데드크로스 (현재가: {current_px:,.0f}, 평단: {avg_price:,.0f} "
            f"수익 {profit_pct:+.2f}%, 구간 {SWING_RSI_FULL_MIN_PROFIT_PCT:.0f}~"
            f"{SWING_RSI_FULL_MAX_PROFIT_PCT:.0f}%)",
        )

    return "HOLD", ""


def check_swing_profit_lock_trailing_exit(
    curr_p: float,
    pos_info: dict,
    *,
    ohlcv=None,
) -> tuple[bool, str]:
    """
    스윙 **수익 보존 락** 이탈만 검사 (하드스탑은 ``check_swing_exit`` 전담).

    ``profit_floor = get_swing_profit_lock_floor(buy, max_p)`` 가 0보다 크고
    현재가가 그 이하이면 전량 청산.
    """
    p = pos_info if isinstance(pos_info, dict) else {}
    cp = float(curr_p)
    buy = _swing_avg_price(p)
    if buy <= 0 or cp <= 0:
        return False, ""
    max_p = max(_finite_price(p.get("max_p", buy), buy), cp)
    profit_floor = get_swing_profit_lock_floor(buy, max_p)
    if profit_floor <= 0:
        return False, ""
    if cp <= float(profit_floor):
        profit_pct = _swing_profit_rate_pct(buy, cp)
        return (
            True,
            f"스윙 수익 보존 락 이탈 (락선 {profit_floor:,.0f}, 수익 {profit_pct:+.2f}%, "
            f"max_p 기준 락 {_swing_profit_lock_tier_label(buy, max_p)})",
        )
    return False, ""


def _swing_profit_lock_tier_label(buy_p: float, max_p: float) -> str:
    """로그용 — 스윙 본절 락 활성화 여부."""
    bp = float(buy_p)
    if bp <= 0:
        return "-"
    max_profit_rate = _max_profit_rate_pct(bp, max_p)
    if max_profit_rate > SWING_PROFIT_LOCK_ACTIVATE_PCT:
        preserve_pct = (BREAKEVEN_LOCK_MULT - 1.0) * 100.0
        return f"최고+{max_profit_rate:.1f}%→본절+{preserve_pct:.1f}%"
    return f"미달(활성화>{SWING_PROFIT_LOCK_ACTIVATE_PCT:.1f}%)"