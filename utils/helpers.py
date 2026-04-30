# -*- coding: utf-8 -*-
"""
크로스컷팅 헬퍼 — 티커 키, 표시 이름, KIS 토큰·매매내역 JSON.

``coin_holding_meets_min_notional`` / ``coin_broker.should_include_coin_balance_row``
    코인 **먼지**는 config ``coin_min_notional_usd``(기본 1 USD) **미만 명목**이면 잔고·GUI·동기화에서 제외.
    가격을 못 구할 때만 ``coin_qty_counts_for_position``(수량 하한)으로 폴백.
    그 외 먼지는 ``get_held_coins``·``sync``·ROI 경로에서 제외되고, ``positions`` 자동복구·유령 정리 규칙은 기존과 같다.

설계 메모
    * ``configure_kis_token_path`` / ``configure_trade_history`` 는 ``run_bot`` 기동 시 한 번 호출.
    * ``get_kr_company_name`` 은 잔고 JSON에서 종목명을 찾기 위해 **``run_bot`` 을 동적 import** 한다
      (순환 참조를 피하려는 역사적 구조 — 리팩터 시 broker 인젝션으로 바꿀 여지 있음).
    * ``record_trade`` 는 스레드 락(``configure_trade_history`` 로 주입)으로 ``trade_history.json`` 에 append.
"""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

# =====================================================================
# 코인 한글명 변환 도우미
# =====================================================================
_coin_name_cache = {}

# KIS 토큰 JSON
_kis_token_path = None

# 매매 내역 JSON
_trade_history_path = None
_trade_history_lock = None

_us_name_cache = {}

# 업비트 코인 잔고 수량이 이 값 **이하**이면 매도 루프·장부 동기화·보유 목록에서 모두 제외(먼지).
# run_bot 코인 매도 루프의 "수량 너무 적음" 패스와 동일 기준.
COIN_MIN_POSITION_QTY = 0.0001


def coin_qty_counts_for_position(qty) -> bool:
    """``COIN_MIN_POSITION_QTY`` 초과일 때만 실질 보유로 본다(가격을 못 구할 때 폴백)."""
    try:
        return float(qty or 0) > COIN_MIN_POSITION_QTY
    except (TypeError, ValueError):
        return False


def coin_holding_meets_min_notional(
    qty: float,
    unit_price: float | None,
    *,
    is_binance: bool,
    min_usd: float,
    krw_per_usdt: float,
) -> bool:
    """
    보유 명목이 최소 기준 이상이면 True.

    * 바이낸스: ``qty × 호가(USDT) >= min_usd`` (기본 min_usd 는 USD·USDT 명목).
    * 업비트: ``qty × 호가(KRW) >= min_usd × krw_per_usdt`` (달러 기준을 원화로 환산).
    * 현재가를 못 구하면 레거시 수량 하한(``coin_qty_counts_for_position``)만 적용.
    """
    try:
        q = float(qty or 0)
    except (TypeError, ValueError):
        return False
    if q <= 0:
        return False
    m = float(min_usd or 1.0)
    if m <= 0:
        m = 1.0
    try:
        px = float(unit_price) if unit_price is not None else 0.0
    except (TypeError, ValueError):
        px = 0.0
    if px > 0:
        if is_binance:
            return q * px >= m
        try:
            kpx = float(krw_per_usdt or 0)
        except (TypeError, ValueError):
            kpx = 0.0
        if kpx > 0:
            return q * px >= m * kpx
    return coin_qty_counts_for_position(q)


def seconds_until_next_quarter_hour(tz_name: str = "Asia/Seoul") -> float:
    """
    다음 벽시계 ``:00 / :15 / :30 / :45`` 분까지 남은 초(해당 타임존 기준).

    GUI ``QTimer.singleShot`` 등으로 **프로세스 시작 시각과 무관하게** 매매 사이클을
    한국장 기준 분 단위에 맞출 때 사용한다.
    """
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    minute = now.minute
    next_slot = ((minute // 15) + 1) * 15
    if next_slot >= 60:
        boundary = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        boundary = now.replace(minute=next_slot, second=0, microsecond=0)
    if boundary <= now:
        boundary = boundary + timedelta(minutes=15)
    return max(0.0, (boundary - now).total_seconds())


def seconds_until_next_half_hour(tz_name: str = "Asia/Seoul") -> float:
    """
    다음 벽시계 ``:00`` 또는 ``:30`` 분(초·마이크로초 0)까지 남은 초(해당 타임존 기준).

    텔레그램 생존신고 등 **30분 간격·벽시계 정렬** 스케줄에 사용한다.
    """
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    minute = now.minute
    if minute < 30:
        boundary = now.replace(minute=30, second=0, microsecond=0)
    else:
        boundary = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    if boundary <= now:
        boundary = boundary + timedelta(minutes=30)
    return max(0.0, (boundary - now).total_seconds())


def kis_equities_weekend_suppress_window_kst(now: datetime | None = None) -> bool:
    """
    한국투자증권(KIS) 주말 정기 점검 등으로 국·미장 잔고 API가 불안정할 때 사용하는 **차단 창**(KST).

    * **차단 구간:** 토요일 08:00 이후 ~ 월요일 07:00 직전 (월요일 07:00 정각부터 해제).
    * 이 안에서는 국장·미장 **REST 잔고·보유 조회를 호출하지 않고**, 장부만 유지한다.
    * **업비트(코인)** 는 이 함수와 무관하게 항상 호출 가능해야 한다.

    반환 ``True`` 이면 KIS 에 자산 조회 요청을 보내지 않는다.
    """
    tz = ZoneInfo("Asia/Seoul")
    n = now or datetime.now(tz)
    if n.tzinfo is None:
        n = n.replace(tzinfo=tz)
    else:
        n = n.astimezone(tz)
    wd = n.weekday()  # 월=0 … 일=6
    minutes = n.hour * 60 + n.minute
    # 월요일 00:00 ~ 06:59
    if wd == 0 and minutes < 7 * 60:
        return True
    # 토요일 08:00 ~
    if wd == 5 and minutes >= 8 * 60:
        return True
    # 일요일 종일
    if wd == 6:
        return True
    return False


def configure_kis_token_path(path):
    global _kis_token_path
    _kis_token_path = path


def configure_trade_history(path, lock):
    global _trade_history_path, _trade_history_lock
    _trade_history_path = path
    _trade_history_lock = lock


def ensure_dict(data):
    """데이터가 딕셔너리가 아니면 빈 딕셔너리 반환"""
    if isinstance(data, dict):
        return data
    return {}


def is_coin_ticker(ticker) -> bool:
    """국·미가 아닌 **코인** 장부 키 (업비트 ``KRW-`` / 바이낸스 ``USDT-``)."""
    v = str(ticker or "").strip().upper()
    return v.startswith("KRW-") or v.startswith("USDT-")


def normalize_ticker(ticker):
    """티커/종목코드를 장부 키 기준으로 정규화합니다."""
    value = str(ticker or "").strip().upper()
    if not value:
        return ""
    if value.startswith("KRW-"):
        return value
    if value.startswith("USDT-"):
        return value
    if value.isdigit():
        return value.zfill(6)
    return value


def get_coin_name(ticker):
    """코인 티커(KRW-BTC)를 한글명(비트코인)으로 변환"""
    global _coin_name_cache

    # 캐시가 비어있다면 업비트에서 전체 코인 목록을 1회만 가져옵니다.
    if not _coin_name_cache:
        try:
            url = "https://api.upbit.com/v1/market/all?isDetails=false"
            resp = requests.get(url, timeout=5).json()
            for item in resp:
                _coin_name_cache[item['market']] = item['korean_name']
        except Exception as e:
            print(f"⚠️ 코인명 조회 실패: {e}")

    if ticker.startswith("USDT-"):
        return ticker.replace("USDT-", "")
    # 캐시에서 이름을 찾고, 만약 없다면 'BTC' 처럼 영어만 잘라서 반환합니다.
    return _coin_name_cache.get(ticker, ticker.replace("KRW-", ""))


def get_us_company_name(ticker):
    """🇺🇸 미국주식 회사명 조회 (초고속 메모리 캐싱 적용)"""
    global _us_name_cache
    if ticker in _us_name_cache:
        return _us_name_cache[ticker]

    try:
        info = yf.Ticker(ticker).info
        name = info.get('longName', ticker)
        _us_name_cache[ticker] = name  # 찾은 이름은 뇌에 저장
        return name
    except:
        return ticker


def get_kr_company_name(code):
    """🇰🇷 국내주식 종목명 조회 (잔고는 run_bot 경유 get_balance_with_retry 사용)"""
    import importlib
    m64 = importlib.import_module("run_bot")
    try:
        bal = ensure_dict(m64.get_balance_with_retry())
        kr_output1 = bal.get('output1', []) if isinstance(bal.get('output1'), list) else []
        for stock in kr_output1:
            if stock.get('pdno') == code:
                return stock.get('prdt_name', code)
        return code
    except:
        return code


def load_kis_token():
    """파일에서 토큰 정보 로드"""
    if _kis_token_path is None:
        return None
    if _kis_token_path.exists():
        with open(_kis_token_path, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return None
    return None


def save_kis_token(token_data):
    """토큰 정보를 파일에 저장"""
    if _kis_token_path is None:
        return
    with open(_kis_token_path, "w") as f:
        json.dump(token_data, f)


def record_trade(trade_info):
    """매매 내역을 JSON 파일에 기록"""
    if _trade_history_path is None or _trade_history_lock is None:
        print("⚠️ record_trade: 경로/락 미설정 (configure_trade_history 호출 필요)")
        return
    with _trade_history_lock:
        history = []
        if _trade_history_path.exists():
            with open(_trade_history_path, 'r', encoding='utf-8') as f:
                try:
                    history = json.load(f)
                except json.JSONDecodeError:
                    history = []

        history.append(trade_info)

        with open(_trade_history_path, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)


def ensure_binance_order_precision(
    internal_ticker: str,
    amount: float | None = None,
    price: float | None = None,
) -> tuple[float | None, float | None]:
    """
    바이낸스 현물 주문 직전 수량·가격 보정.

    ``ccxt`` 마켓의 ``precision`` / ``limits`` 를 반영한다. 수량은 LOT_SIZE step 기준 **내림(floor)** 이
    가능하면 적용해 초과 매도·Invalid Order 를 줄인다.
    """
    from api import binance_api

    ex = binance_api.ensure_exchange()
    sym = binance_api.internal_to_ccxt(internal_ticker)
    m = ex.market(sym)
    out_amt = None
    out_px = None
    if amount is not None and float(amount) > 0:
        amt = float(amount)
        step = None
        try:
            for f in (m.get("info") or {}).get("filters") or []:
                if str(f.get("filterType") or "") == "LOT_SIZE":
                    step = float(f.get("stepSize") or 0)
                    break
        except Exception:
            step = None
        if step and step > 0:
            import math

            amt = math.floor(amt / step) * step
        out_amt = float(ex.amount_to_precision(sym, amt))
        if out_amt <= 0:
            out_amt = None
    if price is not None and float(price) > 0:
        out_px = float(ex.price_to_precision(sym, float(price)))
    return out_amt, out_px
