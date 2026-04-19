# -*- coding: utf-8 -*-
"""
================================================================================
run_bot — V5.0 통합 자동매매 엔진 (국장 / 미장 / 코인)
================================================================================

한 줄 요약
    ``config.json`` 과 ``bot_state.json`` 을 기준으로 한국투자·업비트 API에 주문을 넣고,
    ``strategy/``·``execution/`` 레이어의 규칙을 한곳에서 오케스트레이션한다.

실행 예
    * ``python run_gui.py`` — GUI가 이 모듈을 import; 타이머·버튼으로 동일 엔진 호출.
    * ``python run_bot.py`` — ``main()`` → ``run_continuously()`` 로 주기 루프.

설정 반영
    * ``config.json`` 은 **프로세스 기동 시 한 번만** 읽는다. 수정 후에는 봇/GUI 재시작.

상태 파일
    * ``bot_state.json`` — positions, cooldown, stats, Phase5 합산 고점·서킷 쿨다운 등.
    * ``trade_history.json`` — 체결/이벤트 기록(락 사용).

주요 의존
    * ``api.kis_api`` / ``api.upbit_api`` — 브로커·주문.
    * ``execution.guard`` / ``execution.sync_positions`` — 장부·MDD·실보유 동기화.
    * ``execution.order_twap`` — 대액 시장가 분할 매수(TWAP).
    * ``strategy.rules`` — 진입/청산 시그널·OHLCV.
    * ``strategy.sector_lock`` / ``strategy.ai_filter`` / ``strategy.macro_guard`` — 섹터·휩쏘·거시.

코드 맵
    * 상단 유틸 — 지수 변화율, 미장 유니버스(S&P100+Ndx50, ``us_universe_cache.json``) 등.
    * ``# 0. 기본 설정`` 이후 — 경로, 로깅, config 전역, Phase2~5 플래그.
    * ``run_trading_bot()`` — 한 사이클(동기화 → 손절/익절 → 스크리너 → 신규 매수).
    * ``run_continuously`` / ``start_scanner_scheduler`` — schedule 루프·스캐너(매매는 매시 KST :00/:15/:30/:45).
"""
import time, json, schedule, pyupbit, requests, traceback, threading, sys, os
import pytz
from ta.trend import ADXIndicator
from pathlib import Path
from datetime import datetime, timedelta, time as dt_time
import yfinance as yf
import pandas as pd
import pandas_market_calendars as mcal
import concurrent.futures
from execution.guard import (
    CORE_ASSETS,
    CORE_COIN_ASSETS,
    load_state,
    save_state,
    in_cooldown,
    set_cooldown,
    in_ticker_cooldown,
    set_ticker_cooldown_after_sell,
    ticker_cooldown_human,
    can_open_new,
    check_mdd_break,
    in_account_circuit_cooldown,
    set_account_circuit_cooldown,
    update_peak_equity_total_krw,
)
from execution.circuit_break import evaluate_total_account_circuit, estimate_usdkrw
from execution.sync_positions import sync_all_positions
from strategy.ai_filter import (
    evaluate_false_breakout_filter,
    get_orderbook_summary_for_coin,
    get_orderbook_summary_from_broker,
    get_recent_15m_ohlcv,
)
from strategy.rules import (
    calculate_pro_signals,
    check_pro_exit,
    get_final_exit_price,
    get_ohlcv_yfinance,
    get_ohlcv_realtime,
    get_ohlcv_kis_domestic_daily,
)
from strategy.sector_lock import allow_kr_sector_entry, allow_us_sector_entry, seed_us_sector_cache
from strategy.macro_guard import get_macro_guard_snapshot
from execution.order_twap import plan_krw_slices, plan_usd_slices
import screener

# =====================================================================
# 1. 시장·시총 보조 — 지수 등락률(급락 필터), S&P500 시총 상위(백업 티커 풀)
# =====================================================================
def get_market_index_change(market):
    """시장 지수의 당일 변화율을 조회합니다."""
    try:
        if market == "KR":
            # 🚨 yfinance 대신 딜레이 없는 네이버 증권 API 사용 (KOSPI 실시간)
            url = "https://m.stock.naver.com/api/index/KOSPI/price?pageSize=2&page=1"
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            
            response = requests.get(url, headers=headers, timeout=5)
            data = response.json()
            
            # data[0]은 오늘(실시간), data[1]은 어제 데이터
            if data and len(data) >= 2:
                curr_close = float(data[0]['closePrice'].replace(',', ''))
                prev_close = float(data[1]['closePrice'].replace(',', ''))
                change = ((curr_close - prev_close) / prev_close) * 100
                return change
            return 0.0
            
        elif market == "US":
            ticker = yf.Ticker("^GSPC")
            hist = ticker.history(period="5d")
            if len(hist) >= 2:
                prev_close = hist['Close'].iloc[-2]
                curr_close = hist['Close'].iloc[-1]
                change = ((curr_close - prev_close) / prev_close) * 100
                return change
                
        elif market == "COIN":
            df = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=3)
            if df is not None and len(df) >= 2:
                prev_close = df['close'].iloc[-2]
                curr_close = df['close'].iloc[-1]
                change = ((curr_close - prev_close) / prev_close) * 100
                return change
                
    except Exception as e:
        print(f"  ⚠️ [{market} 지수] 조회 실패: {e}")
        
    return 0.0

import requests
import concurrent.futures # 핵심: 멀티스레딩 라이브러리

US_UNIVERSE_CACHE_FILE = "us_universe_cache.json"
US_UNIVERSE_CACHE_TTL_SEC = 24 * 3600


def _us_ticker_hyphen(sym: str) -> str:
    """yfinance·KIS 공통으로 쓰기 좋게 '.' → '-' (예: BRK.B → BRK-B)."""
    return str(sym or "").strip().upper().replace(".", "-")


def _wiki_table_symbols(url: str, symbol_col_candidates=("Symbol", "Ticker")) -> list[str]:
    import io as _io

    headers = {
        # Wikipedia 는 공란/단순 UA 에 차단·간소 응답을 줄 수 있어 브라우저 UA 로 명시
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36 cbot-universe/1.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = requests.get(url, headers=headers, timeout=45)
    status = resp.status_code
    html = resp.text or ""
    if status != 200 or len(html) < 10_000:
        print(f"  ⚠️ [위키] 응답 이상 — status={status}, len={len(html)} ({url})")

    tables = None
    last_exc: Exception | None = None
    for flavor in ("lxml", "bs4", "html5lib"):
        try:
            tables = pd.read_html(_io.StringIO(html), flavor=flavor)
            if tables:
                break
            tables = None
        except ImportError as e:
            last_exc = e
            continue
        except Exception as e:
            last_exc = e
            continue
    if tables is None:
        raise RuntimeError(
            f"pd.read_html 실패(lxml/bs4/html5lib 모두): {last_exc!r} — "
            f"status={status}, len={len(html)} ({url})"
        )

    for table in tables:
        flat_cols = [str(c).split(".")[-1] for c in table.columns]
        col = None
        for cand in symbol_col_candidates:
            if cand in flat_cols:
                col = table.columns[flat_cols.index(cand)]
                break
        if col is None:
            continue
        out: list[str] = []
        for x in table[col].astype(str).tolist():
            s = _us_ticker_hyphen(x)
            if s and s not in ("NAN", "NAT", ""):
                out.append(s)
        if len(out) >= 90:
            return out
    raise RuntimeError(f"위키 표에서 심볼 열을 찾지 못함: {url}")


def _fetch_market_cap_yf(sym: str) -> tuple[str, float]:
    s = _us_ticker_hyphen(sym)
    try:
        cap = float((yf.Ticker(s).info or {}).get("marketCap") or 0.0)
    except Exception:
        cap = 0.0
    return s, cap


def _fetch_us_sector_gics(sym: str) -> tuple[str, str]:
    s = _us_ticker_hyphen(sym)
    try:
        info = yf.Ticker(s).info or {}
        sec = str(info.get("sector") or info.get("industry") or "").strip() or "Unknown"
    except Exception:
        sec = "Unknown"
    return s, sec


def get_top_market_cap_tickers(limit=150, *, force_refresh: bool = False):
    """
    미장 감시 유니버스: **S&P 500 시총 상위 100 + Nasdaq 100 중 Tier1 제외 상위 50** (총 150).

    * ``us_universe_cache.json`` 에 티커·GICS 섹터를 저장하고 **24시간**마다만 재조회.
    * ``force_refresh=True`` 면 캐시 TTL·기존 파일을 무시하고 즉시 재빌드(미장 스크리너 잡 용).
    * ``seed_us_sector_cache`` 로 섹터를 주입해 ``allow_us_sector_entry`` 와 연동.
    """
    base_dir = Path(__file__).resolve().parent
    cache_path = base_dir / US_UNIVERSE_CACHE_FILE

    if not force_refresh:
        try:
            if cache_path.exists():
                age = time.time() - cache_path.stat().st_mtime
                if age < US_UNIVERSE_CACHE_TTL_SEC:
                    payload = json.loads(cache_path.read_text(encoding="utf-8"))
                    tickers = payload.get("tickers") or []
                    sectors = payload.get("sectors") or {}
                    if 100 <= len(tickers) <= 150 and all(isinstance(x, str) for x in tickers):
                        seed_us_sector_cache(sectors)
                        print(
                            f"  -> ✅ 미장 유니버스 캐시 사용: {len(tickers)}개 "
                            f"(갱신 후 {int(age // 3600)}h, {cache_path.name})"
                        )
                        return tickers
        except Exception as e:
            print(f"  ⚠️ 미장 유니버스 캐시 읽기 실패 — 재빌드: {e}")

    print(
        "  -> ⏳ [미장 유니버스] S&P500 시총 Top100 + Nasdaq100 Tier1 제외 상위 50 "
        f"(총 {limit}개) 빌드 중… (최초·24h마다 yfinance 다발 호출)"
    )

    sp_url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    # Nasdaq-100 은 ``List_of_Nasdaq-100_companies`` 가 404 반환하는 경우가 있어
    # 본 문서(``/wiki/Nasdaq-100``) → ``NASDAQ-100`` → 과거 경로 순으로 폴백한다.
    ndx_urls = [
        "https://en.wikipedia.org/wiki/Nasdaq-100",
        "https://en.wikipedia.org/wiki/NASDAQ-100",
        "https://en.wikipedia.org/wiki/List_of_Nasdaq-100_companies",
    ]

    try:
        sp_raw = _wiki_table_symbols(sp_url)
    except Exception as e:
        print(f"🚨 [미장 유니버스] S&P500 위키 파싱 실패 — 빌드 중단: {e}")
        raise

    ndx_raw: list[str] = []
    ndx_err: Exception | None = None
    for u in ndx_urls:
        try:
            ndx_raw = _wiki_table_symbols(u)
            if ndx_raw:
                break
        except Exception as e:
            ndx_err = e
            print(f"  ⚠️ [미장 유니버스] Nasdaq100 위키 실패({u}): {e}")
            continue
    if not ndx_raw:
        print(
            f"  ⚠️ [미장 유니버스] Nasdaq100 위키 모든 경로 실패 — "
            f"S&P500 Top100 만으로 빌드 진행 (마지막 오류: {ndx_err!r})"
        )

    print("     ... S&P 500 시총 병렬 조회 ...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        sp_caps = list(ex.map(_fetch_market_cap_yf, sp_raw))
    sp_caps = [(s, c) for s, c in sp_caps if c > 0]
    sp_caps.sort(key=lambda x: x[1], reverse=True)
    tier1 = [s for s, _ in sp_caps[:100]]
    tier1_set = set(tier1)

    tier2: list[str] = []
    if ndx_raw:
        print("     ... Nasdaq 100 시총 병렬 조회 ...")
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
                ndx_caps = list(ex.map(_fetch_market_cap_yf, ndx_raw))
            ndx_caps = [(s, c) for s, c in ndx_caps if c > 0]
            ndx_caps.sort(key=lambda x: x[1], reverse=True)
            for s, _c in ndx_caps:
                if s in tier1_set:
                    continue
                tier2.append(s)
                if len(tier2) >= 50:
                    break
        except Exception as e:
            print(f"  ⚠️ [미장 유니버스] Nasdaq 시총 조회 실패 — Tier2 생략: {e}")

    if len(tier2) < 50:
        print(
            f"  ⚠️ [미장 유니버스] Tier2(Nasdaq100) {len(tier2)}개 확보 "
            f"(목표 50). 유니버스가 {len(tier1) + len(tier2)}개로 줄어듭니다."
        )

    try:
        final = (tier1 + tier2)[:limit]
        if len(final) < limit:
            print(f"  ⚠️ [미장 유니버스] 목표 {limit}개 미만 ({len(final)}개) — 그대로 캐시합니다.")
        if not final:
            raise RuntimeError("S&P500/Nasdaq100 모두 비어 유니버스 빌드 불가")

        print(f"     ... GICS 섹터 병렬 조회 ({len(final)}종) ...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
            sec_pairs = list(ex.map(_fetch_us_sector_gics, final))
        sectors = {s: sec for s, sec in sec_pairs}

        payload = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "tickers": final,
            "sectors": sectors,
            "meta": {
                "tier1_n": len(tier1),
                "tier2_n": len(tier2),
                "target_limit": limit,
                "actual_n": len(final),
            },
        }
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        seed_us_sector_cache(sectors)
        print(f"✅ 미장 유니버스 {len(final)}개 빌드·캐시 저장 완료 ({cache_path.name})")
        return final

    except Exception as e:
        print(f"⚠️ [경고] 미장 유니버스 빌드 실패: {e}")
        try:
            if cache_path.exists():
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                tickers = payload.get("tickers") or []
                sectors = payload.get("sectors") or {}
                if 100 <= len(tickers) <= 150:
                    seed_us_sector_cache(sectors)
                    age_h = int((time.time() - cache_path.stat().st_mtime) // 3600)
                    print(
                        f"    -> 🗂️ 기존 캐시 재사용: {len(tickers)}개 "
                        f"(갱신 후 {age_h}h, {cache_path.name}) — 신규 빌드 실패로 폴백"
                    )
                    return tickers
        except Exception as e2:
            print(f"    ⚠️ 기존 캐시 재사용도 실패: {e2}")
        print("    -> 비상용 백업 목록으로 대신합니다.")
        fb = ["QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"]
        seed_us_sector_cache({t: "Unknown" for t in fb})
        return fb[: max(1, min(limit, len(fb)))]

# =====================================================================
# 0. 기본 설정 — 경로, 로깅, 텔레그램, config.json 단일 로드, Phase 전역
# =====================================================================
BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "bot_state.json"
TRADE_HISTORY_PATH = BASE_DIR / "trade_history.json"
KIS_TOKEN_PATH = BASE_DIR / "kis_token.json"
TRADE_HISTORY_LOCK = threading.Lock()

from utils.logger import setup_quant_logging
try:
    setup_quant_logging()
except Exception as e:
    print(f"⚠️ 로깅 설정 실패: {e}")

from utils.telegram import configure_telegram, register_telegram_atexit, send_telegram
from utils.helpers import (
    coin_qty_counts_for_position,
    configure_kis_token_path,
    configure_trade_history,
    ensure_dict,
    kis_equities_weekend_suppress_window_kst,
    normalize_ticker,
    get_coin_name,
    get_kr_company_name,
    get_us_company_name,
    record_trade,
)
configure_kis_token_path(KIS_TOKEN_PATH)
configure_trade_history(TRADE_HISTORY_PATH, TRADE_HISTORY_LOCK)

with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

configure_telegram(config)
register_telegram_atexit()

from api import kis_api, upbit_api
kis_api.configure(config)

from api.kis_api import (
    _create_brokers,
    refresh_brokers_if_needed,
    get_us_cash_real,
    get_real_us_positions,
    get_kis_top_trade_value,
    get_kis_market_cap_rank,
    execute_us_order_direct,
    get_balance_with_retry,
    get_us_positions_with_retry,
    get_valid_order_price,
    create_market_sell_order_kis,
    create_market_buy_order_kis,
)

_scanner_started = False
_schedule_loop_started = False

# ⚙️ [최대 포지션 설정] 장부(bot_state.json)에서 실시간 연동
_tmp_state = load_state(STATE_PATH)
_saved_settings = _tmp_state.get("settings", {})

MAX_POSITIONS_KR = _saved_settings.get("max_pos_kr", 3)      # 기본값 3
MAX_POSITIONS_US = _saved_settings.get("max_pos_us", 3)      # 기본값 3
MAX_POSITIONS_COIN = _saved_settings.get("max_pos_coin", 5)  # 기본값 5

# Phase 3: AI False Breakout filter
AI_FALSE_BREAKOUT_ENABLED = bool(config.get("ai_false_breakout_enabled", True))
AI_FALSE_BREAKOUT_THRESHOLD = int(config.get("ai_false_breakout_threshold", 70))
AI_FALSE_BREAKOUT_THRESHOLD_COIN = int(config.get("ai_false_breakout_threshold_coin", 80))
AI_FALSE_BREAKOUT_PROVIDER = str(config.get("ai_false_breakout_provider", "gemini") or "gemini").strip().lower()

# Phase 5 / Dry-run: config.json — test_mode=true 시 주문 대신 로그·텔레그램만
TEST_MODE = bool(config.get("test_mode", False))
# 합산 자산 서킷(기본 ON). 끄려면 config.json 에 "account_circuit_enabled": false
ACCOUNT_CIRCUIT_ENABLED = bool(config.get("account_circuit_enabled", True))
ACCOUNT_CIRCUIT_MDD_PCT = float(config.get("account_circuit_mdd_pct", 15.0))
ACCOUNT_CIRCUIT_COOLDOWN_H = float(config.get("account_circuit_cooldown_hours", 24.0))

# Phase 2: 대액 시장가 매수 TWAP (원화 500만 / USD 5000 초과 시 분할)
TWAP_ENABLED = bool(config.get("twap_enabled", True))
TWAP_KRW_THRESHOLD = float(config.get("twap_krw_threshold", 5_000_000))
TWAP_USD_THRESHOLD = float(config.get("twap_usd_threshold", 5000))
TWAP_SLICE_DELAY_SEC = float(config.get("twap_slice_delay_sec", 90))
# 매수: 장(또는 일봉 기준) 마감 직전 N분만 허용 (기본 30분). TWAP 분할 시 마감 직후 주문 방지
BUY_WINDOW_MINUTES_BEFORE_CLOSE = int(config.get("buy_window_minutes_before_close", 30))

# Phase 4: VIX / Fear&Greed 거시 방어막 (매 루프 `get_macro_guard_snapshot(config)` 로 적용)
# config: macro_guard_enabled, macro_vix_block_threshold, macro_fgi_reduce_threshold,
#         macro_fgi_budget_multiplier, macro_vix_fallback, macro_fgi_fallback, (선택) macro_*_override

# 📊 [지수 급락 기준] 각 시장의 신규 매수 중단 임계값
INDEX_CRASH_KR = -3.0     # 국장 KOSPI 급락 기준 (%)
INDEX_CRASH_US = -1.8     # 미장 S&P500 급락 기준 (%)
INDEX_CRASH_COIN = -3.5   # 코인 BTC 급락 기준 (%)

# 업비트 코인 시장가 매수 — 가용 잔고 캡(수수료·반올림 오차로 InsufficientFundsBid 방지)
UPBIT_KRW_AVAILABLE_CAP_RATIO = 0.999  # 주문 직전: min(목표액, get_balance(KRW) * 이 값)
UPBIT_COIN_MIN_ORDER_KRW = 5000.0      # KRW 마켓 최소 주문 금액(업비트 기준)

# 종목 명칭 딕셔너리
kr_name_dict = {"005930": "삼성전자", "000660": "SK하이닉스", "035420": "NAVER", "035720": "카카오", "005380": "현대차", "069500": "KODEX 200"}
us_name_dict = {"AAPL": "애플", "MSFT": "마이크로소프트", "NVDA": "엔비디아", "TSLA": "테슬라", "AMZN": "아마존"}

# =====================================================================
# 2. OHLCV 캐시 — 한 사이클 안에서 동일 티커에 대한 yfinance/KIS 반복 호출 감소
# =====================================================================
# 🗄️ OHLCV 캐시 (루프 시작 시 한번에 조회, 이후 재사용)
_ohlcv_cache = {}
_ohlcv_cache_time = 0
_kis_ohlcv_last_ts = 0.0
# 국장 KIS 일봉 연속 호출 간격(초). 실전 유량 여유·모의는 0.55 권장(config.json)
KIS_OHLCV_MIN_INTERVAL_SEC = float(config.get("kis_ohlcv_min_interval_sec", 0.15))


def _throttle_kis_ohlcv():
    """KIS domestic OHLCV TR 연속 호출 시 초당 한도를 넘기지 않도록 간격을 둔다."""
    global _kis_ohlcv_last_ts
    if KIS_OHLCV_MIN_INTERVAL_SEC <= 0:
        return
    gap = time.time() - _kis_ohlcv_last_ts
    need = KIS_OHLCV_MIN_INTERVAL_SEC - gap
    if need > 0:
        time.sleep(need)
    _kis_ohlcv_last_ts = time.time()


def prefetch_ohlcv(tickers, market="KR", broker=None):
    """
    매도 루프 시작 전 보유 종목 OHLCV 일괄 캐싱.

    * **KR** + ``broker`` 있음: **KIS 일봉 우선**(쓰로틀) → 14봉 미만이면 **yfinance 백업**
    * **US** 등: yfinance
    """
    global _ohlcv_cache, _ohlcv_cache_time
    # 캐시가 3분 이내면 재사용
    if time.time() - _ohlcv_cache_time < 180:
        cached_count = sum(1 for t in tickers if t in _ohlcv_cache and _ohlcv_cache[t])
        if cached_count >= len(tickers) * 0.8:
            print(f"  📦 [{market}] OHLCV 캐시 재사용 ({cached_count}/{len(tickers)}개)")
            return

    print(f"  📦 [{market}] OHLCV 일괄 조회 시작 ({len(tickers)}개)...")
    success, fail = 0, 0
    use_kis_first = market == "KR" and broker is not None
    for t in tickers:
        if t in _ohlcv_cache and _ohlcv_cache[t] and len(_ohlcv_cache[t]) >= 14:
            success += 1
            continue
        try:
            ohlcv = None
            if use_kis_first and str(t).isdigit():
                _throttle_kis_ohlcv()
                ohlcv = list(get_ohlcv_kis_domestic_daily(broker, t) or [])
                yf = []
                if len(ohlcv) < 200:
                    yf = get_ohlcv_yfinance(t) or []
                if yf and len(yf) > len(ohlcv):
                    ohlcv = yf
                    print(f"     ✅ [{t}] yfinance로 일봉 확장·백업 ({len(ohlcv)}봉)")
                elif len(ohlcv) < 14 and yf:
                    ohlcv = yf
                    print(f"     ⚠️ [{t}] KIS 미달 → yfinance 백업 ({len(ohlcv)}봉)")
            else:
                ohlcv = get_ohlcv_yfinance(t)

            if ohlcv and len(ohlcv) >= 14:
                _ohlcv_cache[t] = ohlcv
                success += 1
            else:
                print(
                    f"     ⚠️ [{t}] OHLCV 부족 ({len(ohlcv) if ohlcv else 0}봉) — "
                    f"KIS+yfinance 모두 미달"
                )
                _ohlcv_cache[t] = ohlcv or []
                fail += 1
        except Exception as e:
            print(f"     🔴 [{t}] OHLCV 일괄 조회 예외: {type(e).__name__}: {e}")
            _ohlcv_cache[t] = []
            fail += 1
    _ohlcv_cache_time = time.time()
    print(f"  📦 [{market}] OHLCV 캐시 완료: 성공 {success}개, 실패 {fail}개")


def get_cached_ohlcv(ticker, broker=None):
    """캐시에서 OHLCV. 국장은 **KIS 일봉 우선**(쓰로틀) 후 **yfinance 백업**, 미장 등은 yfinance 우선."""
    # 1순위: 캐시 확인
    if ticker in _ohlcv_cache and _ohlcv_cache[ticker] and len(_ohlcv_cache[ticker]) >= 200:
        return _ohlcv_cache[ticker]

    kr_digit = broker is not None and str(ticker).isdigit()

    # 2순위(국장): KIS만 먼저 — 야후 401·크럼 노이즈 회피
    if kr_digit:
        _throttle_kis_ohlcv()
        ohlcv_kis = []
        try:
            ohlcv_kis = get_ohlcv_kis_domestic_daily(broker, ticker) or []
        except Exception as e:
            print(f"     ⚠️ [{ticker}] KIS 일봉 조회 예외: {e}")
        if ohlcv_kis and len(ohlcv_kis) >= 200:
            _ohlcv_cache[ticker] = ohlcv_kis
            return ohlcv_kis
        if ohlcv_kis and len(ohlcv_kis) >= 14:
            _ohlcv_cache[ticker] = ohlcv_kis
            if len(ohlcv_kis) < 200:
                print(f"     ⚠️ [{ticker}] KIS 데이터 200일 미만 ({len(ohlcv_kis)}봉) — yfinance로 보강 시도.")

    # 3순위: yfinance (미장 기본 / 국장 백업·200봉 보강)
    try:
        ohlcv_yf = get_ohlcv_yfinance(ticker)
        if ohlcv_yf and len(ohlcv_yf) >= 200:
            _ohlcv_cache[ticker] = ohlcv_yf
            return ohlcv_yf
        if ohlcv_yf:
            prev = _ohlcv_cache.get(ticker) or []
            if len(ohlcv_yf) > len(prev):
                _ohlcv_cache[ticker] = ohlcv_yf
            if len(ohlcv_yf) < 200:
                print(f"     ⚠️ [{ticker}] yfinance 데이터 200일 미만 ({len(ohlcv_yf)}봉).")
    except Exception as e:
        print(f"     ⚠️ [{ticker}] yfinance 조회 실패: {e}")

    # 최종: 캐시에 쌓인 것 중 최선
    if ticker in _ohlcv_cache and len(_ohlcv_cache[ticker]) > 0:
        return _ohlcv_cache[ticker]

    print(f"     🔴 [{ticker}] OHLCV 데이터 전체 실패 (200일 이상 확보 불가).")
    return []

# =====================================================================
# 3. 유틸리티 — 타입 방어, 계좌번호 파싱, 장부 키 정규화
# =====================================================================
def ensure_list(data):
    """데이터가 리스트가 아니면 빈 리스트 반환"""
    if isinstance(data, list):
        return data
    return []

def _to_float(v, default=0.0) -> float:
    try:
        if v is None: return float(default)
        if isinstance(v, str): v = v.replace(",", "").strip()
        return float(v)
    except (ValueError, TypeError):
        return float(default)


def _upbit_krw_spendable(balances) -> float:
    """
    업비트 KRW **주문 가능** 원화.

    ``get_balances()`` 의 ``balance`` 는 잔고 전체이고, ``locked`` 는 미체결·출금 대기 등에 묶인 금액이다.
    봇이 ``balance`` 만 쓰면 가용보다 크게 잡혀 ``InsufficientFundsBid`` → pyupbit ``buy_market_order`` 가 ``None`` 반환.
    """
    for b in balances or []:
        if str(b.get("currency", "")).upper() == "KRW":
            total = _to_float(b.get("balance", 0), 0.0)
            locked = _to_float(b.get("locked", 0), 0.0)
            return max(0.0, float(total) - float(locked))
    return 0.0

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

def normalize_positions_keys(state):
    """state['positions'] 키를 정규화해 조회 불일치를 방지합니다."""
    positions = state.get("positions", {})
    if not isinstance(positions, dict):
        state["positions"] = {}
        return True

    normalized = {}
    changed = False
    for raw_key, payload in positions.items():
        key = normalize_ticker(raw_key)
        if key != str(raw_key):
            changed = True
        if not key:
            changed = True
            continue
        if key in normalized:
            prev = normalized[key] if isinstance(normalized[key], dict) else {}
            curr = payload if isinstance(payload, dict) else {}
            prev_bt = _to_float(prev.get("buy_time", 0), 0.0)
            curr_bt = _to_float(curr.get("buy_time", 0), 0.0)
            if curr_bt > prev_bt:
                normalized[key] = payload
            changed = True
        else:
            normalized[key] = payload

    if changed or len(normalized) != len(positions):
        state["positions"] = normalized
        return True
    return False

def _fallback_is_market_open_kr(now_local) -> bool:
    """XKRX 캘린더(schedule) 실패 시 근사: KST 평일 정규장 09:00–15:30 (공휴일 미반영)."""
    if int(now_local.weekday()) >= 5:
        return False
    t = now_local.time()
    return dt_time(9, 0) <= t <= dt_time(15, 30)


def _fallback_is_market_open_us(now_local) -> bool:
    """NYSE 캘린더 실패 시 근사: 미동부 평일 09:30–16:00 (공휴일 미반영)."""
    if int(now_local.weekday()) >= 5:
        return False
    t = now_local.time()
    return dt_time(9, 30) <= t <= dt_time(16, 0)


def is_market_open(market="KR"):
    """한국, 미국, 코인 시장의 개장 여부를 확인.

    ``pandas_market_calendars`` 의 ``schedule()`` 이 특정 pandas/캘린더 데이터 조합에서
    ``ValueError: Length of values (...) does not match length of index (...)`` 를 낼 수 있어,
    실패 시 장중 시간대만으로 근사 판별한다 (GUI ``curr_p`` 공유 등이 막히지 않도록).
    """
    if market == "COIN":
        return True

    now_utc = pd.Timestamp.now(tz="UTC")

    if market == "KR":
        now_local = now_utc.tz_convert("Asia/Seoul")
        if now_local.weekday() >= 5:
            return False
        cal_name = "XKRX"
    elif market == "US":
        now_local = now_utc.tz_convert("US/Eastern")
        if now_local.weekday() >= 5:
            return False
        cal_name = "NYSE"
    else:
        return False

    try:
        cal = mcal.get_calendar(cal_name)
        today_str = now_local.strftime("%Y-%m-%d")
        schedule_cal = cal.schedule(start_date=today_str, end_date=today_str)
        if schedule_cal.empty:
            return False
        market_open = schedule_cal.iloc[0]["market_open"]
        market_close = schedule_cal.iloc[0]["market_close"]
        return bool(market_open <= now_utc <= market_close)
    except Exception:
        if market == "KR":
            return _fallback_is_market_open_kr(now_local)
        return _fallback_is_market_open_us(now_local)

def _record_trade_event(market, ticker, side, qty, price=None, profit_rate=None, reason="", name=""):
    """매매 이벤트를 누적 저장용 JSON에 append"""
    symbol_name = str(name or "").strip()
    try:
        if not symbol_name:
            if str(market) == "KR" or str(ticker).isdigit():
                symbol_name = get_kr_company_name(ticker)
            elif str(market) == "US":
                symbol_name = get_us_company_name(ticker)
            elif str(market) == "COIN":
                code = str(ticker or "")
                symbol_name = code.split("-", 1)[1] if code.startswith("KRW-") else code
    except Exception:
        symbol_name = str(name or "").strip()

    record_trade({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market": market,
        "ticker": ticker,
        "name": symbol_name,
        "side": side,
        "qty": qty,
        "price": price,
        "profit_rate": profit_rate,
        "reason": reason,
    })

# =====================================================================
# 3b. KIS 잔고 표시 스냅샷 — 주말 점검 시 GUI·텔레에 직전 성공 값 표시
# =====================================================================
def load_last_kis_display_snapshot() -> dict:
    """``bot_state.json`` 의 ``last_kis_display_snapshot`` (국·미 예수·총평·수익률)."""
    st = load_state(STATE_PATH)
    snap = st.get("last_kis_display_snapshot")
    return snap if isinstance(snap, dict) else {}


def save_last_kis_display_snapshot(
    d2_kr: int,
    kr_total: int,
    kr_hold_roi,
    us_cash: float,
    us_total: float,
    us_hold_roi,
) -> None:
    """
    KIS 조회가 성공한 직후 호출 — 주말 창에서는 덮어쓰지 않음(직전 평일 값 유지).
    """
    if kis_equities_weekend_suppress_window_kst():
        return
    st = load_state(STATE_PATH)
    st["last_kis_display_snapshot"] = {
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kr": {"cash": int(d2_kr), "total": int(kr_total), "roi": kr_hold_roi},
        "us": {"cash": float(us_cash), "total": float(us_total), "roi": us_hold_roi},
    }
    save_state(STATE_PATH, st)


# =====================================================================
# 4. 실보유 조회 — API 실패 시 ``None``, 빈 보유는 ``[]`` (동기화 로직이 구분)
# =====================================================================
def get_held_stocks_kr():
    """🇰🇷 국장 실제 보유 종목 코드 리스트 가져오기
    성공: list 반환 (빈 리스트도 정상)
    실패: None 반환
    """
    if kis_equities_weekend_suppress_window_kst():
        try:
            st = load_state(STATE_PATH)
            pos = st.get("positions") or {}
            return [t for t in pos if str(t).isdigit()]
        except Exception:
            return []
    try:
        bal = get_balance_with_retry()
        if not bal:
            print(f"❌ [국장 조회 실패] 잔고 API 응답 없음")
            return None
        if 'output1' not in bal:
            print(f"❌ [국장 조회 실패] output1 필드 없음")
            return None
        # hldg_qty 우선, 없으면 ccld_qty_smtl1 사용
        held = []
        for s in bal['output1']:
            hldg_qty = _to_float(s.get('hldg_qty', 0))
            ccld_qty = _to_float(s.get('ccld_qty_smtl1', 0))
            # 0.00001보다 큰 경우만 보유한 것으로 인정 (부동소수점 오차 방지)
            if hldg_qty > 0.0001 or ccld_qty > 0.0001:
                code = normalize_ticker(s.get('pdno', ''))
                if code:
                    held.append(code)
        return held
    except Exception as e:
        print(f"❌ [국장 조회 실패] {type(e).__name__}: {e}")
        return None

def get_held_stocks_us():
    """🇺🇸 미장 실제 보유 종목 티커 리스트 가져오기
    성공: list 반환 (빈 리스트도 정상)
    실패: None 반환
    """
    if kis_equities_weekend_suppress_window_kst():
        try:
            st = load_state(STATE_PATH)
            pos = st.get("positions") or {}
            return [t for t in pos if (not str(t).isdigit() and not str(t).upper().startswith("KRW-"))]
        except Exception:
            return []
    try:
        bal = get_us_positions_with_retry()
        if not bal or 'output1' not in bal:
            print(f"❌ [미장 조회 실패] 잔고 API 응답 없음")
            return None
        held = []
        for s in bal['output1']:
            qty = _to_float(s.get('ovrs_cblc_qty', s.get('ccld_qty_smtl1', s.get('hldg_qty', 0))))
            code = normalize_ticker(s.get('ovrs_pdno', s.get('pdno', '')))
            if qty > 0 and code:
                held.append(code)
        return held
    except Exception as e:
        print(f"❌ [미장 조회 실패] {type(e).__name__}: {e}")
        return None

def get_held_coins():
    """🪙 코인 실제 보유 티커 리스트 가져오기
    성공: list 반환 (빈 리스트도 정상)
    실패: None 반환
    """
    try:
        balances = upbit_api.upbit.get_balances()
        if not balances:
            print(f"❌ [코인 조회 실패] 잔고 API 응답 없음")
            return None
        held = [
            f"KRW-{b['currency']}"
            for b in balances
            if b['currency'] not in ['KRW', 'VTHO']
            and coin_qty_counts_for_position(b.get('balance', 0))
        ]
        return held
    except Exception as e:
        print(f"❌ [코인 조회 실패] {type(e).__name__}: {e}")
        return None

# =====================================================================
# 5. 기상청 + MDD 브레이크 (ADX 횡보장 완벽 방어형)
# =====================================================================
def get_real_weather(broker_kr, broker_us):
    """V6.5 실전용 기상청: ADX(추세강도)와 20일선을 활용한 완벽한 횡보장 판독기"""
    weather = {"KR": "☁️ SIDEWAYS", "US": "☁️ SIDEWAYS", "COIN": "☁️ SIDEWAYS"}
    
    try:
        from ta.trend import ADXIndicator
    except ImportError:
        print("🚨 [경고] 'ta' 라이브러리가 없습니다. 터미널에서 'pip install ta'를 실행해주세요. (임시 횡보장 처리)")
        return weather

    import pyupbit

    # -----------------------------------------------------------
    # 🇰🇷 국장 날씨 (KODEX 200 - yfinance 사용으로 KIS API 에러 원천 차단)
    # -----------------------------------------------------------
    try:
        df_kr = yf.Ticker("069500.KS").history(period="2mo")
        if not df_kr.empty and len(df_kr) >= 30:
            # ADX 계산 (기본 14일)
            adx_kr = ADXIndicator(high=df_kr['High'], low=df_kr['Low'], close=df_kr['Close'], window=14)
            df_kr['adx'] = adx_kr.adx()
            
            curr_c = df_kr['Close'].iloc[-1]
            ma20 = df_kr['Close'].rolling(20).mean().iloc[-1]
            curr_adx = df_kr['adx'].iloc[-1]
            
            # ADX가 20 이하면 무조건 횡보장 (매수 금지)
            if pd.isna(curr_adx) or curr_adx < 20:
                weather['KR'] = "☁️ SIDEWAYS"
            else:
                weather['KR'] = "☀️ BULL" if curr_c > ma20 else "🌧️ BEAR"
    except Exception as e: 
        print(f"  ⚠️ 국장 ADX 날씨 판독 실패: {e}")

    # -----------------------------------------------------------
    # 🇺🇸 미장 날씨 (SPY - S&P 500 ETF)
    # -----------------------------------------------------------
    try:
        df_us = yf.Ticker("SPY").history(period="2mo")
        if not df_us.empty and len(df_us) >= 30:
            adx_us = ADXIndicator(high=df_us['High'], low=df_us['Low'], close=df_us['Close'], window=14)
            df_us['adx'] = adx_us.adx()
            
            curr_c = df_us['Close'].iloc[-1]
            ma20 = df_us['Close'].rolling(20).mean().iloc[-1]
            curr_adx = df_us['adx'].iloc[-1]
            
            if pd.isna(curr_adx) or curr_adx < 25:
                weather['US'] = "☁️ SIDEWAYS"
            else:
                weather['US'] = "☀️ BULL" if curr_c > ma20 else "🌧️ BEAR"
    except Exception as e:
        print(f"  ⚠️ 미장 ADX 날씨 판독 실패: {e}")
        
    # -----------------------------------------------------------
    # 🪙 코인 날씨 (비트코인)
    # -----------------------------------------------------------
    try:
        df_coin = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=40)
        if df_coin is not None and len(df_coin) >= 30:
            adx_coin = ADXIndicator(high=df_coin['high'], low=df_coin['low'], close=df_coin['close'], window=14)
            df_coin['adx'] = adx_coin.adx()
            
            curr_c = df_coin['close'].iloc[-1]
            ma20 = df_coin['close'].rolling(20).mean().iloc[-1]
            curr_adx = df_coin['adx'].iloc[-1]
            
            if pd.isna(curr_adx) or curr_adx < 25:
                weather['COIN'] = "☁️ SIDEWAYS"
            else:
                weather['COIN'] = "☀️ BULL" if curr_c > ma20 else "🌧️ BEAR"
    except Exception as e:
        print(f"  ⚠️ 코인 ADX 날씨 판독 실패: {e}")
        
    return weather


# =====================================================================
# GUI용 추가 함수들 (run_gui.py 호환)
# =====================================================================
def _ledger_qty_for_ui(pos_entry, fallback=1.0) -> float:
    """장부 ``positions[ticker]`` 의 ``qty`` (주말 KIS 미조회 시 표시용). 없으면 ``fallback``."""
    if not isinstance(pos_entry, dict):
        return float(fallback)
    q = _to_float(pos_entry.get("qty"), 0.0)
    return float(q) if q > 0 else float(fallback)


def get_held_stocks_kr_info():
    """국내 보유 주식 정보"""
    if kis_equities_weekend_suppress_window_kst():
        try:
            st = load_state(STATE_PATH)
            pos = st.get("positions") or {}
            return [
                {"code": t, "name": kr_name_dict.get(t, t), "qty": _ledger_qty_for_ui(pos.get(t), 1.0)}
                for t in pos
                if str(t).isdigit()
            ]
        except Exception:
            return []
    try:
        bal = get_balance_with_retry()
        if bal and 'output1' in bal:
            return [{'code': s['pdno'], 'name': kr_name_dict.get(s['pdno'], s.get('prdt_name', '')), 'qty': _to_float(s.get('hldg_qty'))} for s in bal['output1'] if _to_float(s.get('hldg_qty')) > 0]
        return []
    except: return []

def get_held_stocks_us_info():
    """미국 보유 주식 정보"""
    if kis_equities_weekend_suppress_window_kst():
        try:
            st = load_state(STATE_PATH)
            pos = st.get("positions") or {}
            return [
                {"code": t, "name": us_name_dict.get(t, t), "qty": _ledger_qty_for_ui(pos.get(t), 1.0)}
                for t in pos
                if not str(t).isdigit() and not str(t).upper().startswith("KRW-")
            ]
        except Exception:
            return []
    try:
        bal = get_us_positions_with_retry()
        if bal and 'output1' in bal:
            return [{'code': s['ovrs_pdno'], 'name': us_name_dict.get(s['ovrs_pdno'], s.get('ovrs_item_name', '')), 'qty': _to_float(s.get('ovrs_cblc_qty'))} for s in bal['output1'] if _to_float(s.get('ovrs_cblc_qty')) > 0]
        return []
    except: return []

def get_held_stocks_us_detail():
    """미국 보유 주식 상세 (GUI용으로 변환)"""
    if kis_equities_weekend_suppress_window_kst():
        try:
            st = load_state(STATE_PATH)
            pos = st.get("positions") or {}
            out = []
            for code, p in pos.items():
                if str(code).isdigit() or str(code).upper().startswith("KRW-"):
                    continue
                bp = _to_float(p.get("buy_p", 0), 0.0)
                out.append(
                    {
                        "code": code,
                        "qty": _ledger_qty_for_ui(p, 1.0),
                        "avg_p": bp,
                        "current_p": bp,
                    }
                )
            return out
        except Exception:
            return []
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
                # 현재가 추출 추가
                current_p = _to_float(item.get('ovrs_now_pric1', item.get('now_pric2', 0)))
                
                result.append({
                    'code': item.get('ovrs_pdno', item.get('pdno', '')),
                    'qty': qty,
                    'avg_p': _to_float(item.get('ovrs_avg_pric', item.get('ovrs_avg_unpr', item.get('avg_unpr3', 0)))),
                    'current_p': current_p  # 현재가 필드 추가
                })
        return result
    except:
        return []

def get_held_stocks_coins_info():
    """코인 보유 정보"""
    try:
        balances = upbit_api.upbit.get_balances()
        coins = []
        for b in balances:
            if b['currency'] not in ('KRW', 'VTHO'):
                qty = _to_float(b.get('balance'))
                if coin_qty_counts_for_position(qty):
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
            if kis_equities_weekend_suppress_window_kst():
                return {}
            try: return get_balance_with_retry()
            except: return {}
        elif market == "US":
            if kis_equities_weekend_suppress_window_kst():
                return {}
            try: return get_us_positions_with_retry()
            except: return {}
        elif market == "COIN":
            try: return upbit_api.upbit.get_balances()
            except: return []
        return {}
    
    # 현재: dict 값 추출 모드
    if isinstance(data, dict):
        return data.get(key, default)
    return default


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
    set_ticker_cooldown_after_sell(state, ticker, "수동 매도", profit_rate=profit_rate)
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
            # 현재가 먼저 조회
            ohlcv = get_ohlcv_realtime(kis_api.broker_kr, code)
            curr_p = _to_float(ohlcv[-1].get('c', 0), 0.0) if ohlcv else 0.0
            if curr_p <= 0:
                return {"success": False, "message": "국장 현재가 조회 실패"}
            
            resp = create_market_sell_order_kis(code, int(qty), is_us=False, curr_price=curr_p)
            ok = isinstance(resp, dict) and resp.get("rt_cd") == "0"
            msg = resp.get("msg1", "국장 시장가 매도 요청") if isinstance(resp, dict) else "국장 매도 응답 없음"
            if ok:
                exec_price = curr_p
                profit_rate = _apply_manual_sell_state_update(code, exec_price)
                _record_trade_event("KR", code, "SELL", int(qty), price=exec_price if exec_price > 0 else None, profit_rate=profit_rate, reason="MANUAL")
                kr_name = get_kr_company_name(code)
                profit_str = f"{profit_rate:+.2f}%" if profit_rate is not None else "N/A"
                print(f"  ✅ [국장 수동매도 체결] {kr_name}({code}) {int(qty)}주 | 수익률: {profit_str}")
                send_telegram(f"✅ [KR] {code}({kr_name}) {int(qty)}주 수동 매도 완료")
                return {"success": True, "message": msg}
            return {"success": False, "message": msg}

        if market == "US":
            # 수동매도는 시장가로 처리
            us_bal = (
                {}
                if kis_equities_weekend_suppress_window_kst()
                else ensure_dict(get_us_positions_with_retry())
            )
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
            resp = execute_us_order_direct(kis_api.broker_us, "sell", code, int(qty), current_price)
            ok = isinstance(resp, dict) and resp.get("rt_cd") == "0"
            msg = resp.get("msg1", "미장 시장가 매도 요청") if isinstance(resp, dict) else "미장 매도 응답 없음"
            if ok:
                profit_rate = _apply_manual_sell_state_update(code, current_price)
                _record_trade_event("US", code, "SELL", int(qty), price=current_price, profit_rate=profit_rate, reason="MANUAL")
                us_name = get_us_company_name(code)
                profit_str = f"{profit_rate:+.2f}%" if profit_rate is not None else "N/A"
                print(f"  ✅ [미장 수동매도 체결] {us_name}({code}) {int(qty)}주 | 수익률: {profit_str}")
                send_telegram(f"✅ [US] {code}({us_name}) {int(qty)}주 수동 매도 완료")
                return {"success": True, "message": msg}
            return {"success": False, "message": msg}

        if market == "COIN":
            current_p = _to_float(pyupbit.get_current_price(code), 0.0)
            resp = upbit_api.upbit.sell_market_order(code, qty)
            if resp:
                profit_rate = _apply_manual_sell_state_update(code, current_p)
                _record_trade_event("COIN", code, "SELL", qty, price=current_p if current_p > 0 else None, profit_rate=profit_rate, reason="MANUAL")
                profit_str = f"{profit_rate:+.2f}%" if profit_rate is not None else "N/A"
                print(f"  ✅ [코인 수동매도 체결] {code} {qty} | 수익률: {profit_str}")
                send_telegram(f"✅ [COIN] {code} {qty} 수동 매도 완료")
                return {"success": True, "message": "코인 시장가 매도 요청 완료"}
            return {"success": False, "message": "코인 매도 응답 없음"}

        return {"success": False, "message": f"지원하지 않는 시장 코드: {market}"}
    except Exception as e:
        err = str(e)
        send_telegram(f"🚨 [{market}] {code} 수동 매도 실패: {err}")
        return {"success": False, "message": err}


def _portfolio_total_krw_from_aux(state: dict) -> float:
    """직전 루프에서 저장한 시장별 합산 스냅샷 + 현재 환율로 원화 합산."""
    rate = estimate_usdkrw()
    kr = float(state.get("circuit_aux_last_kr_krw", 0) or 0)
    coin = float(state.get("circuit_aux_last_coin_krw", 0) or 0)
    usd = float(state.get("circuit_aux_last_usd_total", 0) or 0)
    return kr + coin + usd * rate


def _phase5_emergency_liquidate_all(state: dict) -> None:
    """
    합산 서킷 발동 시 비코어 전량 시장가 매도(단발).
    부분 TWAP는 장부/부분체결 정합 이슈로 Phase2에서 정식화 예정.
    """
    if TEST_MODE:
        msg = "🧪 [TEST_MODE] Phase5 전량 청산 — 실주문 생략 (수동 매도 경로 미호출)"
        print(f"  {msg}")
        try:
            lines = []
            if not kis_equities_weekend_suppress_window_kst():
                bal = ensure_dict(get_balance_with_retry())
                for stock in ensure_list(bal.get("output1")):
                    code = normalize_ticker(stock.get("pdno", ""))
                    qty = int(_to_float(stock.get("hldg_qty", 0)))
                    if qty > 0 and code and code not in CORE_ASSETS:
                        lines.append(f"KR {code} x{qty}")
                us_bal = ensure_dict(get_us_positions_with_retry())
                for item in ensure_list(us_bal.get("output1")):
                    c = normalize_ticker(item.get("ovrs_pdno", item.get("pdno", "")))
                    q = int(_to_float(item.get("ovrs_cblc_qty", item.get("hldg_qty", 0))))
                    if q > 0 and c and c not in CORE_ASSETS:
                        lines.append(f"US {c} x{q}")
            for b in upbit_api.upbit.get_balances() or []:
                if b.get("currency") in ("KRW", "VTHO"):
                    continue
                t = f"KRW-{b['currency']}"
            qf = float(_to_float(b.get("balance", 0)))
            if coin_qty_counts_for_position(qf) and t not in CORE_ASSETS:
                lines.append(f"COIN {t} x{qf}")
            send_telegram(f"{msg}\n대상:\n" + "\n".join(lines[:40]))
        except Exception as e:
            print(f"  ⚠️ [TEST_MODE] 청산 시뮬 요약 실패: {e}")
        return

    # KR
    if not kis_equities_weekend_suppress_window_kst():
        try:
            bal = ensure_dict(get_balance_with_retry())
            for stock in ensure_list(bal.get("output1")):
                code = normalize_ticker(stock.get("pdno", ""))
                qty = int(_to_float(stock.get("hldg_qty", 0)))
                if qty <= 0 or not code or code in CORE_ASSETS:
                    continue
                manual_sell("KR", code, qty)
        except Exception as e:
            print(f"  ⚠️ [Phase5] 국장 전량 청산 루프 예외: {e}")

    # US
    if not kis_equities_weekend_suppress_window_kst():
        try:
            us_bal = ensure_dict(get_us_positions_with_retry())
            for item in ensure_list(us_bal.get("output1")):
                c = normalize_ticker(item.get("ovrs_pdno", item.get("pdno", "")))
                q = int(_to_float(item.get("ovrs_cblc_qty", item.get("hldg_qty", 0))))
                if q <= 0 or not c or c in CORE_ASSETS:
                    continue
                manual_sell("US", c, q)
        except Exception as e:
            print(f"  ⚠️ [Phase5] 미장 전량 청산 루프 예외: {e}")

    # COIN
    try:
        for b in upbit_api.upbit.get_balances() or []:
            if b.get("currency") in ("KRW", "VTHO"):
                continue
            t = f"KRW-{b['currency']}"
            if t in CORE_ASSETS:
                continue
            qf = float(_to_float(b.get("balance", 0)))
            if not coin_qty_counts_for_position(qf):
                continue
            manual_sell("COIN", t, qf)
    except Exception as e:
        print(f"  ⚠️ [Phase5] 코인 전량 청산 루프 예외: {e}")


def _maybe_run_account_circuit(state: dict) -> None:
    """매 루프 시작부: 합산 MDD 서킷 → (옵션) 전량 청산 + 24h 매수 쿨다운."""
    if not ACCOUNT_CIRCUIT_ENABLED:
        return
    total = _portfolio_total_krw_from_aux(state)
    if total <= 0:
        return

    peak = update_peak_equity_total_krw(state, total, STATE_PATH)
    if in_account_circuit_cooldown(state):
        print(
            f"  🛡️ [Phase5 서킷] 계좌 단위 쿨다운 중 — 신규 매수는 쿨다운 종료까지 차단 "
            f"(until={state.get('account_circuit_cooldown_until', '')})"
        )
        return

    ev = evaluate_total_account_circuit(peak, total, trigger_drawdown_pct=ACCOUNT_CIRCUIT_MDD_PCT)
    print(
        f"  🛡️ [Phase5 서킷] 합산 {total:,.0f}원 (고점 {peak:,.0f}) "
        f"DD={ev['drawdown_pct']:.2f}% / 임계 {ACCOUNT_CIRCUIT_MDD_PCT:g}% → "
        f"{'발동' if ev['triggered'] else '정상'} | {ev['reason']}"
    )
    if not ev["triggered"]:
        return

    send_telegram(
        f"🚨 [Phase5 계좌 서킷 발동]\n{ev['reason']}\n전량 청산을 시도합니다. "
        f"(TEST_MODE={TEST_MODE})"
    )
    _phase5_emergency_liquidate_all(state)
    st2 = load_state(STATE_PATH)
    set_account_circuit_cooldown(st2, STATE_PATH, ACCOUNT_CIRCUIT_COOLDOWN_H)


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
        total_invested = 0.0
        total_current = 0.0
        for b in balances:
            if b['currency'] in ('KRW', 'VTHO'):
                continue
            qty = _to_float(b.get('balance', 0))
            if not coin_qty_counts_for_position(qty):
                continue
            ticker = f"KRW-{b['currency']}"
            # 현재가
            curr_price = pyupbit.get_current_price(ticker) or 0
            current = qty * curr_price
            total_current += current

            # 매수 평균가
            avg_buy_price = _to_float(b.get('avg_buy_price', 0))
            if avg_buy_price > 0:
                invested = qty * avg_buy_price
                total_invested += invested

        # ROI 계산
        profit = total_current - total_invested
        roi = (profit / total_invested * 100) if total_invested > 0 else 0.0
        
        return {"invested": total_invested, "current": total_current, "profit": profit, "roi": roi}
    except: return {"invested": 0.0, "current": 0.0, "profit": 0.0, "roi": 0.0}

def persist_position_registration(state, ticker, position_payload, context="", state_path=STATE_PATH):
    """매수 직후 장부 저장을 재시도하며 검증합니다. (자동복구와 별개)"""
    ticker = normalize_ticker(ticker)
    if not ticker:
        print(f"  ❌ [{context}] 장부 등록 실패: 빈 티커")
        return False

    state.setdefault("positions", {})[ticker] = position_payload
    set_cooldown(state, ticker)

    for attempt in range(1, 4):
        try:
            save_state(state_path, state)
            latest = load_state(state_path)
            latest_positions = latest.get("positions", {}) if isinstance(latest, dict) else {}
            if ticker in latest_positions:
                print(f"  ✅ [{context}] 장부 등록 확인: {ticker} (시도 {attempt}/3)")
                return True
            print(f"  ⚠️ [{context}] 저장 후 미반영: {ticker} (시도 {attempt}/3)")
        except Exception as e:
            print(f"  ⚠️ [{context}] 장부 저장 예외 (시도 {attempt}/3): {e}")

        if attempt < 3:
            time.sleep(0.2)

    print(f"  ❌ [{context}] 장부 등록 최종 실패: {ticker}")
    return False


def ensure_position_registered(ticker, payload, context=""):
    """매수 직후 장부 반영 여부 검증 (복구/수정 없음)"""
    try:
        ticker = normalize_ticker(ticker)
        latest = load_state(STATE_PATH)
        positions = latest.get("positions", {}) if isinstance(latest, dict) else {}
        if ticker in positions:
            print(f"  ✅ [{context}] 장부 등록 확인: {ticker}")
            return True

        print(f"  ❌ [{context}] 장부 등록 실패 감지: {ticker}")
        return False
    except Exception as e:
        print(f"  ⚠️ [{context}] 장부 등록 검증 실패: {e}")
        return False


def _twap_krw_budget_slices(total_krw: float) -> list:
    if not TWAP_ENABLED:
        return [float(total_krw)]
    return plan_krw_slices(float(total_krw), threshold_krw=TWAP_KRW_THRESHOLD)


def _twap_usd_budget_slices(total_usd: float) -> list:
    if not TWAP_ENABLED:
        return [float(total_usd)]
    return plan_usd_slices(float(total_usd), threshold_usd=TWAP_USD_THRESHOLD)


def _execute_kr_market_buy_twap(
    t: str,
    kr_name: str,
    target_budget: float,
    curr_p: float,
    sl_p: float,
    t_name: str,
    s_name: str,
    state: dict,
    kr_cash_holder: list,
) -> bool:
    """시장가 매수(Phase2 분할). 성공 시 장부 1회 등록. TEST_MODE 시 로그만."""
    slices = _twap_krw_budget_slices(target_budget)
    if len(slices) > 1:
        print(
            f"  📉 [Phase2 TWAP KR] {kr_name}({t}) 예산 {int(target_budget):,}원 → {len(slices)}분할 "
            f"(잔여예수 추정 {int(kr_cash_holder[0]):,}원)"
        )

    total_qty = 0
    total_cost = 0.0
    fp = float(curr_p)
    any_fill = False

    for si, krw_slice in enumerate(slices):
        if krw_slice <= 0 or fp <= 0:
            continue
        q = int(float(krw_slice) / fp)
        if q <= 0:
            continue
        est = int(q * fp)
        if int(kr_cash_holder[0]) < est:
            print(f"  ⏭️ [KR TWAP] 슬라이스 {si + 1}/{len(slices)} 예수 부족으로 중단")
            break

        if TEST_MODE:
            print(f"  🧪 TEST_MODE [KR TWAP {si + 1}/{len(slices)}] {kr_name}({t}) qty={q} (~{est:,}원)")
            send_telegram(f"🧪 TEST_MODE KR TWAP {t} ({kr_name}) {si + 1}/{len(slices)} qty={q}")
            total_qty += q
            total_cost += q * fp
            kr_cash_holder[0] = float(int(kr_cash_holder[0]) - est)
            any_fill = True
        else:
            resp = None
            retry_count = 0
            max_retries = 3
            while retry_count < max_retries:
                resp = create_market_buy_order_kis(t, q, is_us=False, curr_price=fp)
                print(
                    f"  🧾 [KR BUY TWAP {si + 1}/{len(slices)}] {t} rt_cd={resp.get('rt_cd')} msg={resp.get('msg1', '')}"
                )
                if isinstance(resp, dict) and resp.get("rt_cd") == "0":
                    break
                retry_count += 1
                if retry_count < max_retries:
                    print(f"  ⚠️ {kr_name}({t}) TWAP 슬라이스 실패 (#{retry_count}): {resp.get('msg1', '')} → 재시도")
                    time.sleep(1)

            if not resp or resp.get("rt_cd") != "0":
                msg1 = (resp or {}).get("msg1", "API 오류")
                if "credentials_type" in str(msg1):
                    print("  🔄 [토큰 오류] 토큰 갱신 후 TWAP 슬라이스 재시도...")
                    refresh_brokers_if_needed(force=True)
                    time.sleep(1)
                    resp = create_market_buy_order_kis(t, q, is_us=False, curr_price=fp)
                    print(f"  🧾 [KR BUY TWAP 재시도] {t} rt_cd={resp.get('rt_cd')}")
                if not resp or resp.get("rt_cd") != "0":
                    print(f"  ❌ [KR TWAP] {kr_name}({t}) 슬라이스 {si + 1} 최종 실패: {msg1}")
                    break

            try:
                output = resp.get("output", {}) if isinstance(resp, dict) else {}
                ord_pric = _to_float(output.get("ORD_PRIC", 0), 0.0)
                if ord_pric > 0:
                    fp = float(ord_pric)
            except Exception:
                pass
            total_qty += q
            total_cost += q * fp
            kr_cash_holder[0] = float(int(kr_cash_holder[0]) - int(q * fp))
            any_fill = True

        if si < len(slices) - 1 and TWAP_SLICE_DELAY_SEC > 0:
            time.sleep(TWAP_SLICE_DELAY_SEC)

    if not any_fill or total_qty <= 0:
        return False

    wavg = total_cost / total_qty if total_qty else fp
    print(f"  ✅ [국장 매수 체결 TWAP] {kr_name}({t}) | 가중평단 ~{int(wavg):,}원 × {total_qty}주 | 손절가: {int(sl_p):,}원")
    send_telegram(
        f"🎯 [{t_name} 매수 TWAP] {t}({kr_name})\n가중평단: ~{int(wavg):,}원 × {total_qty}주 | 손절가: {int(sl_p):,}원\n전략: {s_name}"
    )
    payload = {"buy_p": wavg, "sl_p": sl_p, "max_p": wavg, "tier": t_name, "buy_time": time.time(), "qty": float(total_qty)}
    persist_position_registration(state, t, payload, context="KR BUY TWAP")
    try:
        _record_trade_event("KR", t, "BUY", total_qty, price=wavg, profit_rate=None, reason=s_name)
    except Exception as log_err:
        print(f"  ⚠️ [KR BUY TWAP] 매매내역 기록 실패: {log_err}")
    ensure_position_registered(t, state.get("positions", {}).get(t, {}), context="KR BUY TWAP")
    return True


def _execute_us_market_buy_twap(
    t: str,
    us_name: str,
    target_budget_usd: float,
    curr_p: float,
    sl_p: float,
    t_name: str,
    s_name: str,
    state: dict,
    us_cash_holder: list,
) -> bool:
    slices = _twap_usd_budget_slices(target_budget_usd)
    if len(slices) > 1:
        print(
            f"  📉 [Phase2 TWAP US] {us_name}({t}) 예산 ${target_budget_usd:,.2f} → {len(slices)}분할 "
            f"(현금 ${us_cash_holder[0]:.2f})"
        )

    total_qty = 0
    total_cost = 0.0
    fp = float(curr_p)
    any_fill = False

    for si, usd_slice in enumerate(slices):
        if usd_slice <= 0 or fp <= 0:
            continue
        q = int(float(usd_slice) / fp)
        if q <= 0:
            continue
        buy_price = round(fp * 1.01, 2)
        est = q * fp
        if us_cash_holder[0] < est * 0.99:
            print(f"  ⏭️ [US TWAP] 슬라이스 {si + 1}/{len(slices)} 달러 예수 부족으로 중단")
            break

        if TEST_MODE:
            print(f"  🧪 TEST_MODE [US TWAP {si + 1}/{len(slices)}] {us_name}({t}) qty={q} (~${est:.2f})")
            send_telegram(f"🧪 TEST_MODE US TWAP {t} ({us_name}) {si + 1}/{len(slices)} qty={q}")
            total_qty += q
            total_cost += est
            us_cash_holder[0] = float(us_cash_holder[0] - est)
            any_fill = True
        else:
            resp = None
            retry_count = 0
            max_retries = 3
            while retry_count < max_retries:
                resp = execute_us_order_direct(kis_api.broker_us, "buy", t, q, buy_price)
                print(
                    f"  🧾 [US BUY TWAP {si + 1}/{len(slices)}] {t} rt_cd={resp.get('rt_cd')} msg={resp.get('msg1', '')}"
                )
                if isinstance(resp, dict) and resp.get("rt_cd") == "0":
                    break
                retry_count += 1
                if retry_count < max_retries:
                    time.sleep(1)
            if not resp or resp.get("rt_cd") != "0":
                msg1 = (resp or {}).get("msg1", "API 오류")
                if "credentials_type" in str(msg1).lower() or "token" in str(msg1).lower():
                    print("  🔄 [토큰 오류] 미장 TWAP 슬라이스 재시도...")
                    refresh_brokers_if_needed(force=True)
                    time.sleep(1)
                    resp = execute_us_order_direct(kis_api.broker_us, "buy", t, q, buy_price)
                if not resp or resp.get("rt_cd") != "0":
                    print(f"  ❌ [US TWAP] {us_name}({t}) 슬라이스 실패: {msg1}")
                    break
            total_qty += q
            total_cost += q * fp
            us_cash_holder[0] = float(us_cash_holder[0] - q * fp)
            any_fill = True

        if si < len(slices) - 1 and TWAP_SLICE_DELAY_SEC > 0:
            time.sleep(TWAP_SLICE_DELAY_SEC)

    if not any_fill or total_qty <= 0:
        return False

    wavg = total_cost / total_qty if total_qty else fp
    print(f"  ✅ [미장 매수 체결 TWAP] {us_name}({t}) | ~${wavg:.2f} × {total_qty}주 | 손절: ${sl_p:.2f}")
    send_telegram(f"🎯 [S&P500 매수 TWAP] {t}({us_name})\n가중평단: ~${wavg:.2f} × {total_qty}주\n전략: {s_name}")
    payload = {"buy_p": wavg, "sl_p": sl_p, "max_p": wavg, "tier": t_name, "buy_time": time.time(), "qty": float(total_qty)}
    persist_position_registration(state, t, payload, context="US BUY TWAP")
    try:
        _record_trade_event("US", t, "BUY", total_qty, price=wavg, profit_rate=None, reason=s_name)
    except Exception as log_err:
        print(f"  ⚠️ [US BUY TWAP] 매매내역 기록 실패: {log_err}")
    ensure_position_registered(t, state.get("positions", {}).get(t, {}), context="US BUY TWAP")
    return True


def _execute_coin_market_buy_twap(
    t: str,
    budget_krw: float,
    sl_p: float,
    s_name: str,
    state: dict,
    krw_bal_holder: list,
    held_coins_mut: list[str],
) -> bool:
    slices = _twap_krw_budget_slices(budget_krw)
    if len(slices) > 1:
        print(f"  📉 [Phase2 TWAP COIN] {t} 예산 {int(budget_krw):,}원 → {len(slices)}분할")

    spent = 0.0
    last_p = float(pyupbit.get_current_price(t) or 0.0)
    any_fill = False

    for si, krw_slice in enumerate(slices):
        if krw_slice <= 0:
            continue
        if krw_bal_holder[0] < float(krw_slice):
            print(f"  ⏭️ [COIN TWAP] 슬라이스 {si + 1}/{len(slices)} KRW 부족으로 중단")
            break

        if TEST_MODE:
            print(f"  🧪 TEST_MODE [COIN TWAP {si + 1}/{len(slices)}] {t} {int(krw_slice):,}원")
            send_telegram(f"🧪 TEST_MODE COIN TWAP {t} {si + 1}/{len(slices)} {int(krw_slice):,}KRW")
            spent += float(krw_slice)
            krw_bal_holder[0] = float(krw_bal_holder[0]) - float(krw_slice)
            any_fill = True
        else:
            # 주문 직전 실시간 가용 원화(pyupbit: 주문 가능액) + 수수료/오차용 0.1% 여유 캡
            avail_raw = upbit_api.upbit.get_balance("KRW")
            available_krw = float(avail_raw) if avail_raw is not None else float(krw_bal_holder[0])
            target_buy_amount = float(min(float(krw_slice), float(krw_bal_holder[0])))
            safe_ceiling = available_krw * UPBIT_KRW_AVAILABLE_CAP_RATIO
            final_order_amount = min(target_buy_amount, safe_ceiling)
            pay_krw = int(max(0, final_order_amount))
            if pay_krw < UPBIT_COIN_MIN_ORDER_KRW:
                print(
                    f"  ⏭️ [COIN TWAP] 슬라이스 {si + 1}/{len(slices)} 스킵 — "
                    f"최종주문액 {pay_krw:,}원 < 업비트 최소 {int(UPBIT_COIN_MIN_ORDER_KRW):,}원 "
                    f"(목표 {target_buy_amount:,.0f}원, 가용·API {available_krw:,.0f}원×{UPBIT_KRW_AVAILABLE_CAP_RATIO})"
                )
                break
            if pay_krw < int(target_buy_amount):
                print(
                    f"  🛡️ [COIN TWAP] 가용 캡 적용: 목표 {target_buy_amount:,.0f}원 → 최종 {pay_krw:,}원 "
                    f"(가용 {available_krw:,.0f}원×{UPBIT_KRW_AVAILABLE_CAP_RATIO})"
                )
            resp = upbit_api.upbit.buy_market_order(t, pay_krw)
            print(
                f"  🧾 [COIN BUY TWAP {si + 1}/{len(slices)}] {t} "
                f"pay={pay_krw:,}원 resp={'OK' if resp else 'None'}"
            )
            if not resp:
                print(
                    f"  ❌ [COIN TWAP] {t} 슬라이스 실패 — 업비트 거절(잔고·최소주문·수수료). "
                    f"가용 KRW는 get_balance('KRW') 기준으로 캡 후에도 거절 시 잔고·출금대기 확인."
                )
                break
            spent += float(pay_krw)
            after_raw = upbit_api.upbit.get_balance("KRW")
            krw_bal_holder[0] = (
                float(after_raw) if after_raw is not None else float(krw_bal_holder[0]) - float(pay_krw)
            )
            any_fill = True
            np = pyupbit.get_current_price(t)
            if np:
                last_p = float(np)

        if si < len(slices) - 1 and TWAP_SLICE_DELAY_SEC > 0:
            time.sleep(TWAP_SLICE_DELAY_SEC)

    if not any_fill or spent <= 0 or last_p <= 0:
        return False

    coin_qty = spent / last_p
    coin_name = get_coin_name(t)
    p_fmt = f"{last_p:,.4f}" if last_p < 100 else f"{int(last_p):,}"
    sl_fmt = f"{sl_p:,.4f}" if sl_p < 100 else f"{int(sl_p):,}"
    print(f"  ✅ [코인 매수 체결 TWAP] {t}({coin_name}) | {p_fmt}원 × {coin_qty:.4f} | 손절가: {sl_fmt}원")
    send_telegram(
        f"🎯 [코인 TWAP 매수] {t}({coin_name})\n평단: {p_fmt}원 × {coin_qty:.4f} | 손절: {sl_fmt}원\n전략: {s_name}"
    )
    payload = {"buy_p": last_p, "sl_p": sl_p, "max_p": last_p, "tier": s_name, "buy_time": time.time(), "qty": float(coin_qty)}
    persist_position_registration(state, t, payload, context="COIN BUY TWAP")
    try:
        _record_trade_event("COIN", t, "BUY", spent, price=last_p, profit_rate=None, reason=s_name)
    except Exception as log_err:
        print(f"  ⚠️ [COIN BUY TWAP] 매매내역 기록 실패: {log_err}")
    ensure_position_registered(t, state.get("positions", {}).get(t, {}), context="COIN BUY TWAP")
    if t not in held_coins_mut:
        held_coins_mut.append(t)
    return True


def get_kr_holdings_with_roi():
    """🇰🇷 국장 보유 종목 + 현재 수익률 (balance API 현재가 사용)"""
    try:
        state = load_state(STATE_PATH)
        if kis_equities_weekend_suppress_window_kst():
            holdings = []
            for code, pos in (state.get("positions") or {}).items():
                if not str(code).isdigit():
                    continue
                buy_p = _to_float(pos.get("buy_p", 0), 0)
                if buy_p <= 0:
                    continue
                curr_p = buy_p
                try:
                    oc = get_ohlcv_yfinance(code)
                    if oc and len(oc) > 0:
                        curr_p = float(oc[-1]["c"])
                except Exception:
                    pass
                roi = ((curr_p - buy_p) / buy_p) * 100
                kr_name = get_kr_company_name(code)
                holdings.append(f"  {code}({kr_name}): {int(curr_p):,}원 | {roi:+.2f}% (주말·yfinance)")
            return holdings
        bal = ensure_dict(get_balance_with_retry())
        kr_output1 = bal.get('output1', []) if isinstance(bal.get('output1'), list) else []
        
        holdings = []
        for stock in kr_output1:
            code = normalize_ticker(stock.get('pdno', ''))
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
            ticker = normalize_ticker(item['code'])
            qty = item['qty']
            buy_p = item['avg_p']
            
            if buy_p <= 0:
                continue
            
            # 현재가: yfinance → fallback으로 get_ohlcv_yfinance
            curr_p = buy_p
            try:
                
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

def get_coin_holdings_with_roi():
    """🪙 코인 보유 종목 + 현재 수익률"""
    try:
        state = load_state(STATE_PATH)
        balances = upbit_api.upbit.get_balances() or []
        
        holdings = []
        for b in balances:
            if b['currency'] in ('KRW', 'VTHO'):
                continue
            qty = _to_float(b.get('balance', 0))
            if not coin_qty_counts_for_position(qty):
                continue

            ticker = f"KRW-{b['currency']}"
            pos = state.get('positions', {}).get(ticker, {})
            buy_p = _to_float(pos.get('buy_p', 0), 0)

            # 매수가가 없으면 avg_buy_price 사용
            if buy_p <= 0:
                buy_p = _to_float(b.get('avg_buy_price', 0), 0)

            if buy_p <= 0:
                continue

            # 현재가 조회
            curr_p = pyupbit.get_current_price(ticker) or buy_p

            roi = ((curr_p - buy_p) / buy_p) * 100
            coin_name = get_coin_name(ticker)
            holdings.append(f"  {ticker}({coin_name}): {curr_p:,.0f}원 | {roi:+.2f}%")

        return holdings
    except Exception as e:
        print(f"⚠️ 코인 보유종목 조회 에러: {e}")
        return []

def heartbeat_report():
    """모든 자산 현황을 종합하여 텔레그램으로 보고 (GUI와 동일한 로직)"""
    print("💓 생존 신고 보고서 생성 중...")
    try:
        # 시장 날씨 조회 (20일선 기준)
        weather = get_real_weather(kis_api.broker_kr, kis_api.broker_us)

        _snap = load_last_kis_display_snapshot()

        # ===== 국장 =====
        kr_cash = 0
        kr_total = 0
        kr_roi = None
        if kis_equities_weekend_suppress_window_kst():
            kr_d = _snap.get("kr") or {}
            if isinstance(kr_d, dict) and "total" in kr_d:
                kr_cash = int(kr_d.get("cash", 0))
                kr_total = int(kr_d["total"])
                kr_roi = kr_d.get("roi")
        else:
            try:
                kr_bal = get_balance_with_retry()
                if kr_bal is None:
                    kr_bal = {}

                if "output2" in kr_bal:
                    out2 = kr_bal["output2"]
                    if isinstance(out2, list) and len(out2) > 0:
                        kr_cash = int(_to_float(out2[0].get("prvs_rcdl_excc_amt", 0)))
                    elif isinstance(out2, dict):
                        kr_cash = int(_to_float(out2.get("prvs_rcdl_excc_amt", 0)))

                kr_metrics = _calc_kr_holdings_metrics(kr_bal)
                kr_roi = kr_metrics.get("roi")

                try:
                    out2 = kr_bal.get("output2", [])
                    if isinstance(out2, list) and out2:
                        kr_total = int(_to_float(out2[0].get("tot_evlu_amt"), kr_cash))
                    elif isinstance(out2, dict):
                        kr_total = int(_to_float(out2.get("tot_evlu_amt"), kr_cash))
                except Exception:
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
        if kis_equities_weekend_suppress_window_kst():
            us_d = _snap.get("us") or {}
            if isinstance(us_d, dict) and "total" in us_d:
                us_cash = float(us_d.get("cash", 0))
                us_total = float(us_d["total"])
                us_roi = us_d.get("roi")
        else:
            try:
                us_cash = _safe_num(get_us_cash_real(kis_api.broker_us), 0.0)
                us_bal = get_us_positions_with_retry() or {}
                us_metrics = _calc_us_holdings_metrics(us_bal)
                us_roi = us_metrics.get("roi")

                if us_cash <= 0 and isinstance(us_bal, dict):
                    out2 = us_bal.get("output2", [])
                    if isinstance(out2, list) and out2:
                        us_cash = _safe_num(
                            out2[0].get("frcr_dncl_amt_2", out2[0].get("frcr_buy_amt_smtl", 0)), 0.0
                        )
                    elif isinstance(out2, dict):
                        us_cash = _safe_num(
                            out2.get("frcr_dncl_amt_2", out2.get("frcr_buy_amt_smtl", 0)), 0.0
                        )

                us_stock_value = float(us_metrics.get("current", 0.0) or 0.0)
                us_total = us_cash + us_stock_value
            except Exception as e:
                print(f"  ⚠️ 미장 조회 실패: {e}")
                us_cash = 0.0
                us_total = 0.0

        if not kis_equities_weekend_suppress_window_kst():
            try:
                save_last_kis_display_snapshot(
                    int(kr_cash),
                    int(kr_total),
                    kr_roi,
                    float(us_cash),
                    float(us_total),
                    us_roi,
                )
            except Exception:
                pass

        # ===== 코인 =====
        krw_bal = 0
        coin_total = 0
        coin_roi = None
        try:
            krw_bal = int(_safe_num(upbit_api.upbit.get_balance("KRW"), 0.0))
            coin_bals = upbit_api.upbit.get_balances() or []
            
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
        coin_holdings = get_coin_holdings_with_roi()
        
        kr_holdings_str = "\n".join(kr_holdings) if kr_holdings else "  (보유 없음)"
        us_holdings_str = "\n".join(us_holdings) if us_holdings else "  (보유 없음)"
        coin_holdings_str = "\n".join(coin_holdings) if coin_holdings else "  (보유 없음)"
        
        msg = f"""💓 [3콤보 생존신고]
{weather['KR']} 🇰🇷 국장 | 예수금: {kr_cash:,}원 | 총평가: {kr_total:,}원 | 수익률: {kr_roi_str}
[국장 보유]
{kr_holdings_str}

{weather['US']} 🇺🇸 미장 | 예수금: ${us_cash:,.2f} | 총평가: ${us_total:,.2f} | 수익률: {us_roi_str}
[미장 보유]
{us_holdings_str}

{weather['COIN']} 🪙 코인 | 예수금: {krw_bal:,}원 | 총평가: {coin_total:,}원 | 수익률: {coin_roi_str}
[코인 보유]
{coin_holdings_str}"""
        if kis_equities_weekend_suppress_window_kst():
            sat = (_snap.get("saved_at") or "").strip()
            if sat:
                msg += f"\n📌 국·미 평가는 저장된 직전 조회({sat}) 기준입니다."
        send_telegram(msg)
        print("  ✅ 텔레그램 보고 완료")
    except Exception as e:
        print(f"⚠️ 보고 에러: {e}")
        import traceback
        traceback.print_exc()

# =====================================================================
# 6. 메인 매매 엔진 — ``run_trading_bot()`` 한 번이 곧 한 사이클(매도→매수 파이프라인)
# =====================================================================
def run_trading_bot():
    """
    한 번의 **트레이딩 사이클**을 수행한다 (스케줄러가 주기적으로 호출).

    순서 개요
        1) ``bot_state`` 로드·키 정규화, 브로커 토큰 갱신.
        2) 실계좌 vs 장부 ``sync_all_positions`` (누락 자동복구·유령 삭제·평단 보정).
        3) 시장 날씨·거시 방어막·지수 급락 필터.
        4) 보유 종목 위주 손절/익절·(조건 시) TWAP 청산 등.
        5) 스크리너 후보에 대해 섹터락·AI필터·매수 윈도·TWAP 매수.

    주의
        장시간 블로킹 호출(API·yfinance)이 포함되므로, 호출 주기와 겹치지 않게 설정한다.
    """
    print("\n" + "="*55)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🤖 V5.0 통합 자동매매 봇 가동...")
    print("="*55)

    state = load_state(STATE_PATH)
    if normalize_positions_keys(state):
        save_state(STATE_PATH, state)
        print("  🔧 [장부 정규화] positions 키 포맷 정리 완료")
    refresh_brokers_if_needed()

    # 0) 실보유/장부 동기화 (누락 종목 자동복구 + 유령 삭제)
    # 장부 조회 실패시 갱신금지 (API 실패시 전혀 반영하지 않음)
    held_kr = get_held_stocks_kr()
    held_us = get_held_stocks_us()
    held_coins = get_held_coins()
    
    if held_kr is not None and held_us is not None and held_coins is not None:
        sync_all_positions(state, held_kr, held_us, held_coins, STATE_PATH)
    else:
        failed_apis = []
        if held_kr is None: failed_apis.append("국장")
        if held_us is None: failed_apis.append("미장")
        if held_coins is None: failed_apis.append("코인")
        error_msg = f"실보유 조회 실패 ({', '.join(failed_apis)} API 오류)"
        print(f"  ⚠️ [장부 동기화 건너뜀] {error_msg} - 기존 장부 유지")
    
    weather = get_real_weather(kis_api.broker_kr, kis_api.broker_us)
    print(f"🌡️ 시장 날씨: 국장 {weather['KR']} / 미장 {weather['US']} / 코인 {weather['COIN']}")

    _macro_snap = get_macro_guard_snapshot(config)
    macro_mult = float(_macro_snap.get("budget_multiplier", 1.0))
    if _macro_snap.get("enabled"):
        print(
            f"  🛡️ [Phase4 거시] VIX={float(_macro_snap.get('vix') or 0):.2f} ({_macro_snap.get('vix_source')}) "
            f"FGI={_macro_snap.get('fgi')} ({_macro_snap.get('fgi_source')}) "
            f"-> {_macro_snap.get('mode')} (예산×{macro_mult}) | {_macro_snap.get('reason')}"
        )
    else:
        print(f"  🛡️ [Phase4 거시] 비활성 | {_macro_snap.get('reason', '')}")

    _maybe_run_account_circuit(state)
    state = load_state(STATE_PATH)

    try:
        with open(BASE_DIR / "kr_targets.json", "r", encoding="utf-8") as f:
            scanned_targets = json.load(f)
    except Exception:
        scanned_targets = []

    # 1) 국장 타겟 구성 (조건검색 베이스 필터링)
    market_cap_200 = get_kis_market_cap_rank(kis_api.broker_kr, limit=200)
    time.sleep(1.0)
    realtime_trade_all = get_kis_top_trade_value(kis_api.broker_kr)
    
    # 편의를 위해 거래대금 상위 50위까지만 컷
    top_vol_50 = realtime_trade_all[:50] 

    tier_1 = []
    tier_2 = []
    tier_3 = []
    
    # 모든 종목은 반드시 scanned_targets(조건검색 결과) 안에 있어야 함!
    for t in scanned_targets:
        is_large_cap = t in market_cap_200
        is_high_vol = t in top_vol_50
        
        if is_large_cap and is_high_vol:
            tier_1.append(t)  # 시총 O, 거래대금 O (최상급 주도주)
        elif is_large_cap and not is_high_vol:
            tier_2.append(t)  # 시총 O, 거래대금 X (무거운 우량주)
        elif not is_large_cap and is_high_vol:
            tier_3.append(t)  # 시총 X, 거래대금 O (가벼운 급등주)

    # 최종 타겟 취합 (이미 조건검색 베이스라 중복 제거할 필요도 없음)
    final_targets = tier_1 + tier_2 + tier_3
    
    print(f"  -> 🌐 [국장 타겟] 1티어({len(tier_1)}개) 포함 총 {len(final_targets)}개")


    if is_market_open("KR") and not kis_equities_weekend_suppress_window_kst():
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

        state["circuit_aux_last_kr_krw"] = float(total_kr_equity)
        save_state(STATE_PATH, state)

        kr_output1 = bal.get('output1', []) if isinstance(bal.get('output1'), list) else []
        
        # ✅ [버그 수정] 수량이 0보다 큰 종목만 held_kr에 포함시킵니다.
        held_kr = []
        for s in kr_output1:
            qty = _to_float(s.get('hldg_qty', s.get('t01', s.get('q', 0))))
            if qty > 0.0001:
                code = normalize_ticker(s.get('pdno', ''))
                if code: held_kr.append(code)
        # 매도는 MDD와 무관하게 항상 실행 (손실 방어)
        positions_count = len([
            code for code in held_kr
            if code in state.get("positions", {}) and code not in CORE_ASSETS
        ])
        print(f"  🔍 [국장 매도 루프] 보유 포지션 {positions_count}개 손익 체크 시작...")
        if positions_count == 0:
            print(f"  ✅ [국장 매도 루프] 매도할 종목 없음 (완료)")
        else:
            # 🗄️ OHLCV 일괄 캐싱 (yfinance 우선)
            kr_sell_tickers = [normalize_ticker(s.get('pdno', '')) for s in kr_output1 if normalize_ticker(s.get('pdno', '')) in held_kr and normalize_ticker(s.get('pdno', '')) not in CORE_ASSETS]
            prefetch_ohlcv(kr_sell_tickers, market="KR", broker=kis_api.broker_kr)
        for stock in kr_output1:
            t = normalize_ticker(stock.get('pdno', ''))
            if not t:
                continue
            qty = int(_to_float(stock.get('hldg_qty', stock.get('t01', stock.get('q', 0)))))
            if qty <= 0 or t not in held_kr:
                continue
            if t in CORE_ASSETS:
                print(f"  ⏭️  [{t}] CORE_ASSETS(True) - 스킵")
                continue
            if t not in state.get("positions", {}):
                avg_p = _to_float(stock.get('pchs_avg_prc', stock.get('pchs_avg_pric', stock.get('prpr', 0))), 0.0)
                if avg_p <= 0:
                    avg_p = _to_float(stock.get('prpr', 0), 0.0)
                if avg_p > 0:
                    payload = {
                        'buy_p': float(avg_p),
                        'sl_p': float(avg_p * 0.9),
                        'max_p': float(avg_p),
                        'tier': '자동등록(보유종목)',
                        'buy_time': time.time(),
                        'buy_date': datetime.now().isoformat()
                    }
                    state.setdefault("positions", {})[t] = payload
                    save_state(STATE_PATH, state)
                    print(f"  🚨 [{t}] positions 미조회 → 즉시 자동등록 (buy_p={avg_p:,.2f}, sl_p={avg_p*0.9:,.2f})")
                else:
                    print(f"  ⏭️  [{t}] positions 미조회 + 평단/현재가 없음 - 스킵")
                    continue
            try:
                ohlcv = get_cached_ohlcv(t, broker=kis_api.broker_kr)
                
                if not ohlcv or not isinstance(ohlcv, list) or not ohlcv[-1] or 'c' not in ohlcv[-1]:
                    print(f"  ❌ [KR 매도 루프 예외] {t}: OHLCV 데이터 또는 종가(c) 정보 부족. 건너뜁니다.")
                    continue

                pos_info = state.get("positions", {}).get(t, {})
                
                # 🔄 [완전 동기화] GUI가 장부에 공유한 최신 가격을 최우선으로 사용
                gui_p = pos_info.get('curr_p')
                if gui_p and float(gui_p) > 0:
                    curr_p = float(gui_p)
                else:
                    # GUI 가격이 없을 때만 직접 조회 (Fallback)
                    curr_p = float(ohlcv[-1]['c'])
                    try:
                        _price_resp = kis_api.broker_kr.fetch_price(t)
                        if _price_resp and _price_resp.get('rt_cd') == '0':
                            _realtime_p = float(_price_resp.get('output', {}).get('stck_prpr', 0))
                            if _realtime_p > 0: curr_p = _realtime_p
                    except Exception: pass

                buy_p = pos_info.get('buy_p', curr_p)
                max_p = pos_info.get('max_p', curr_p)
                hard_stop = float(pos_info.get('sl_p', buy_p * 0.9))
                profit_rate_now = ((curr_p - buy_p) / buy_p) * 100 if buy_p > 0 else 0.0

                # 📊 [상태 로그] 한눈에 보기
                kr_name = get_kr_company_name(t)
                chandelier_p = get_final_exit_price(t, curr_p, pos_info, ohlcv)
                print(f"  📊 [KR 보유] {kr_name}({t}) | 현재가: {int(curr_p):,}원 | 매수가: {int(buy_p):,}원 | 최고가: {int(max_p):,}원 | 매도선: {int(chandelier_p):,}원 | 수익률: {profit_rate_now:+.2f}%")

                # 손절가 체크 로그
                if profit_rate_now < 0:
                    print(f"  ⚠️  [{t}] 손실 구간: 수익률 {profit_rate_now:.2f}% (현재가: {curr_p:,.0f} / 손절가: {hard_stop:,.0f})")
                    if curr_p <= hard_stop:
                        print(f"     ➜ 손절 체크: 현재가 {curr_p:,.0f} ≤ 손절가 {hard_stop:,.0f} = 🔴 매도 신호!")

                # 0%~+1% 구간은 신규 매수 후 15분간만 매도 보류
                buy_time = pos_info.get('buy_time', time.time() - 900)
                if 0 <= profit_rate_now < 1.0 and (time.time() - buy_time < 900):
                    continue

                # 매도 결정 로직 (우선순위: 타임스탑 > 하드스탑 > 샹들리에)
                reason = ""
                is_exit = False
                # 1. 현재 시간 및 매수 시간 추출
                now = datetime.now()
                now_str = now.strftime('%Y-%m-%d %H:%M:%S') # 현재(매도 체크) 시간

                buy_date_str = pos_info.get('buy_date')
                buy_time_ts = pos_info.get('buy_time')

                hours_held = 0
                buy_time_log = "알 수 없음"

                # 2. 보유 시간 계산 (buy_date 우선, 없으면 buy_time 사용)
                if buy_date_str:
                    try:
                        buy_datetime = datetime.fromisoformat(buy_date_str)
                        hours_held = (now - buy_datetime).total_seconds() / 3600
                        buy_time_log = buy_datetime.strftime('%Y-%m-%d %H:%M:%S')
                    except:
                        pass

                # buy_date가 없거나 파싱에 실패했는데 buy_time은 있는 경우 ("278470" 케이스 해결)
                if hours_held == 0 and buy_time_ts:
                    hours_held = (time.time() - buy_time_ts) / 3600
                    # 타임스탬프를 사람이 읽을 수 있는 날짜 포맷으로 변환
                    buy_time_log = datetime.fromtimestamp(buy_time_ts).strftime('%Y-%m-%d %H:%M:%S')

                # 📊 상태 로그 출력 (매수 날짜 및 보유 시간 포함)
                print(f"📊 [{now_str}] {t} 상태 체크")
                print(f"   ⏱️ 매수일시: {buy_time_log} ➔ 보유시간: {hours_held:.1f}시간")

                # 168시간(7일) 경과 & 수익률 10% 미만일 경우
                if hours_held >= 168:
                    if profit_rate_now < 10.0:
                        is_exit = True
                        reason = f"타임 스탑 발동 (보유 {hours_held:.1f}시간 / 수익률 {profit_rate_now:+.2f}%)"
                    else:
                        # 수익률이 10% 이상이라 홀딩할 때의 로그
                        print(f"   ✅ {hours_held:.1f}시간 경과했으나 수익률 양호({profit_rate_now:+.2f}%) ➔ 홀딩 유지")
                        

                # 2. 하드스탑 체크 (타임스탑이 발동되지 않았을 때만)
                if not is_exit and profit_rate_now < 0:
                    if curr_p <= hard_stop:
                        is_exit = True
                        reason = "하드스탑 이탈 (손실구간 방어)"
                        print(f"🔴 [하드스탑 발동] {t} - 현재가: {curr_p:,.0f}원 <= 손절가: {hard_stop:,.0f}원. 강제 청산! (is_exit={is_exit})")

                # 3. 샹들리에 엑싯 체크 (타임스탑, 하드스탑 모두 발동되지 않았을 때만)
                if not is_exit and profit_rate_now >= 0: # 수익 구간일 때만 샹들리에 검사
                    is_exit, reason_chandelier = check_pro_exit(t, curr_p, pos_info, ohlcv)
                    if is_exit: # 샹들리에가 True를 반환하면 reason 업데이트
                        reason = reason_chandelier

                if is_exit: # 여기서 실제 매도 주문이 나감
                    kr_name = get_kr_company_name(t)  # 종목명 미리 조회
                    qty = int(_to_float(stock.get('hldg_qty', stock.get('t01', stock.get('q', 0)))))
                    if qty <= 0:
                        continue
                    
                    # 매도 주문 (최대 3회 재시도)
                    retry_count = 0
                    max_retries = 3
                    resp = None
                    
                    while retry_count < max_retries:
                        resp = create_market_sell_order_kis(t, qty, is_us=False, curr_price=curr_p)
                        if resp.get('rt_cd') == '0':
                            break
                        retry_count += 1
                        if retry_count < max_retries:
                            print(f"  ⚠️ {kr_name}({t}) 매도 실패 (#{retry_count}): {resp.get('msg1', 'API 오류')} → 재시도")
                            time.sleep(1)
                    
                    if resp and resp.get('rt_cd') == '0':
                        profit_rate = ((curr_p - buy_p) / buy_p) * 100 if buy_p > 0 else 0.0
                        stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})
                        if profit_rate > 0:
                            stats["wins"] = int(stats.get("wins", 0) or 0) + 1
                        else:
                            stats["losses"] = int(stats.get("losses", 0) or 0) + 1
                        stats["total_profit"] = float(stats.get("total_profit", 0.0) or 0.0) + float(profit_rate)
                        _record_trade_event("KR", t, "SELL", qty, price=curr_p, profit_rate=profit_rate, reason=reason)
                        print(f"  ✅ [국장 매도 체결] {kr_name}({t}) | 수익률: {profit_rate:+.2f}% | 사유: {reason}")
                        send_telegram(f"🚨 [국장 추세종료 매도] {t}({kr_name})\n사유: {reason}\n최종 수익률: {profit_rate:.2f}%")
                        del state["positions"][t]
                        set_cooldown(state, t)
                        set_ticker_cooldown_after_sell(state, t, reason)
                        save_state(STATE_PATH, state)
                    else:
                        print(f"  ❌ {kr_name}({t}) 매도 최종 실패 ({retry_count}회 시도): {resp.get('msg1', 'API 오류') if resp else '응답 없음'}")

            except Exception as e:
                print(f"  ❌ [KR 매도 루프 예외] {t}: {e}")
                traceback.print_exc()
                continue
                
        # 매수는 MDD → Phase4 거시 체크 후에만 실행
        if not check_mdd_break("KR", total_kr_equity, state, STATE_PATH):
            print("  -> 🚨 국장 MDD 브레이크 작동 중. 신규 매수 중단.")
        elif macro_mult <= 0:
            print(f"  -> 🚨 국장 Phase4 거시 방어막: 신규 매수 중단. ({_macro_snap.get('reason', '')})")
        elif in_account_circuit_cooldown(state):
            print("  -> 🚨 국장 Phase5 계좌 서킷 쿨다운 — 신규 매수 중단.")
        else:
            # ⏳ [핵심] 국장 매수: KRX 정규장 마감(15:30 KST) 직전 N분만 (기본 30분 → 15:00~15:29)
            now_kr = datetime.now(pytz.timezone("Asia/Seoul"))
            _kr_close = now_kr.replace(hour=15, minute=30, second=0, microsecond=0)
            _kr_buy_start = _kr_close - timedelta(minutes=BUY_WINDOW_MINUTES_BEFORE_CLOSE)
            is_kr_buy_time = _kr_buy_start <= now_kr < _kr_close

            if not is_kr_buy_time:
                print(
                    f"  ⏳ [KR 매수 대기] 장 마감 {BUY_WINDOW_MINUTES_BEFORE_CLOSE}분 전 구간만 매수 "
                    f"({_kr_buy_start.strftime('%H:%M')}~{_kr_close.strftime('%H:%M')} KST, "
                    f"현재 {now_kr.strftime('%H:%M')})"
                )
            else:
                # 지수 급락 체크
                kr_index_change = get_market_index_change("KR")
                print(f"  📊 [KOSPI 지수] 변화율: {kr_index_change:+.2f}% 날씨는 {weather['KR']}")
                if kr_index_change <= INDEX_CRASH_KR:
                    print(f"  🚫 [KR 매수 중단] KOSPI {kr_index_change:+.2f}% 급락 (기준: {INDEX_CRASH_KR}%)")
                elif weather['KR'] == "🌧️ BEAR":
                    print(f"  🛑 [KR 매수 중단] 현재 국장 날씨는 {weather['KR']} 입니다. (현금 관망)")
                else:
                    total_kr = len(final_targets)
                    print(f"  -> 🇰🇷 국장 사냥감 {total_kr}개 정밀 분석 시작!")
                    for idx, t in enumerate(final_targets, 1):
                        kr_name = get_kr_company_name(t)  # 종목명 미리 조회
                        if in_ticker_cooldown(state, t):
                            print(
                                f"  ⏭️ {kr_name}({t}): 매도 후 쿨다운(톱날 방지) 만료 "
                                f"{ticker_cooldown_human(state, t)} 이전 (패스)"
                            )
                            continue
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
                            if not can_open_new(t, state, max_positions=MAX_POSITIONS_KR):
                                print(
                                    f"  ⏭️ {kr_name}({t}): [MAX_POSITIONS:KR] "
                                    f"국장 한도 {MAX_POSITIONS_KR}개 도달 (패스)"
                                )
                                continue
                            sector_ok_kr, sector_msg_kr = allow_kr_sector_entry(
                                t,
                                state.get("positions", {}),
                                MAX_POSITIONS_KR,
                                normalize_ticker,
                            )
                            if not sector_ok_kr:
                                print(f"  ⏭️ {kr_name}({t}): {sector_msg_kr} (패스)")
                                continue
                        
                        try:
                            ohlcv_200 = get_ohlcv_yfinance(t)
                            
                            # 🛡️ [수술 1] 개별 종목 ADX 폭발 시 하락장 무시 및 비중 상향 로직
                            ratio = 0.10 # 기본 비중
                            try:
                                if ohlcv_200 is not None and len(ohlcv_200) >= 15:
                                    df = pd.DataFrame(ohlcv_200)
                                    adx_indicator = ADXIndicator(
                                        high=df['h'], low=df['l'], close=df['c'], window=14
                                    )
                                    adx_val = adx_indicator.adx().iloc[-1]
                                    
                                    # ADX 25 이상: 강한 추세 발생 (하락장이라도 독고다이 매수 허용)
                                    if adx_val >= 25:
                                        ratio, t_name = 0.60, "독고다이-ADX폭발"
                                    else:
                                        if weather['KR'] == "☀️ BULL":
                                            if t in tier_1: ratio, t_name = 0.60, "1티어(우량대장)-불장"
                                            elif t in tier_2: ratio, t_name = 0.40, "2티어(수급급등)-불장"
                                            else: ratio, t_name = 0.10, "3티어(기타/패턴)-불장"
                                        elif weather['KR'] == "☁️ SIDEWAYS":
                                            if t in tier_1: ratio, t_name = 0.40, "1티어(우량대장)-횡보"
                                            elif t in tier_2: ratio, t_name = 0.30, "2티어(수급급등)-횡보"
                                            else: ratio, t_name = 0.10, "3티어(기타/패턴)-횡보"
                                        else:
                                            # 하락장(BEAR)인데 ADX도 낮으면 매수 안함
                                            if weather['KR'] == "🌧️ BEAR": continue 
                                            ratio, t_name = 0.10, "기타-방어"
                                else:
                                    if weather['KR'] == "🌧️ BEAR": continue
                                    ratio, t_name = 0.10, "기타-방어"
                            except Exception:
                                if weather['KR'] == "🌧️ BEAR": continue
                                ratio, t_name = 0.10, "기타-방어"

                            # 🛡️ [수술 4] 시드 확장 대비 동적 비율 캡(Dynamic Cap) 로직
                            max_allowed_ratio = 1.0 / max(1, MAX_POSITIONS_KR)
                            ratio = min(ratio, max_allowed_ratio)
                            
                            target_budget = total_kr_equity * ratio * macro_mult
                            kr_min_budget = 50000.0 # 국장 최소 주문 (5만원)

                            # 🧹 [수술 2] 예수금 영끌(Sweep) 및 철통 방어 로직
                            if target_budget < kr_min_budget:
                                continue
                            if kr_cash < kr_min_budget:
                                continue
                            if kr_cash < target_budget:
                                print(f"  🧹 [예수금 영끌 발동] {kr_name}({t}): 예산({int(target_budget):,}원) 부족. 지갑에 남은 전액({int(kr_cash):,}원) 풀매수 장전!")
                                target_budget = kr_cash

                            is_buy, sl_p, s_name = calculate_pro_signals(ohlcv_200, weather['KR'], t, kr_name, idx, total_kr)
                            if not is_buy:
                                continue
                            
                            # 🚨 [추가 수술] 국장 전용 시가 갭(Gap) 과다 상승 필터 (5%)
                            try:
                                if ohlcv_200 and len(ohlcv_200) >= 2:
                                    last_close = float(ohlcv_200[-2]['c']) # 어제 종가
                                    today_open = float(ohlcv_200[-1]['o']) # 오늘 시가
                                    if last_close > 0:
                                        gap_ratio = ((today_open - last_close) / last_close) * 100
                                        if gap_ratio >= 5.0:
                                            print(f"  ⏭️ {kr_name}({t}): 갭상승 과다 ({gap_ratio:.2f}%) - 필터링 (패스)")
                                            continue
                            except Exception as gap_err:
                                print(f"  ⚠️ 갭상승 체크 중 오류: {gap_err}")

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

                            # Phase 3: 모든 필터 통과 후 "매수 직전" AI 휩쏘 게이트
                            if AI_FALSE_BREAKOUT_ENABLED:
                                candles_15m = get_recent_15m_ohlcv(t, market="KR", count=10)
                                orderbook = get_orderbook_summary_from_broker(kis_api.broker_kr, t)
                                ai_eval = evaluate_false_breakout_filter(
                                    ticker=t,
                                    candles_15m_10=candles_15m,
                                    orderbook=orderbook,
                                    threshold=AI_FALSE_BREAKOUT_THRESHOLD,
                                    use_ai=True,
                                    ai_provider=AI_FALSE_BREAKOUT_PROVIDER,
                                    config=config,
                                )
                                if ai_eval.get("blocked"):
                                    print(
                                        f"  ⏭️ {kr_name}({t}): [AI FILTER] 가짜돌파 {ai_eval.get('false_breakout_prob')}% "
                                        f"> {AI_FALSE_BREAKOUT_THRESHOLD}% ({ai_eval.get('provider')}) - 패스"
                                    )
                                    continue
                            
                            # 매수 주문 (Phase2 TWAP: 대액 시 분할)
                            kr_box = [float(kr_cash)]
                            _execute_kr_market_buy_twap(
                                t, kr_name, float(target_budget), curr_p, sl_p, t_name, s_name, state, kr_box
                            )
                            kr_cash = int(kr_box[0])
                        except Exception as e:
                            print(f"  ❌ [KR BUY 예외] {t}: {type(e).__name__}: {e}")
                            traceback.print_exc()
                            continue        
    else:
        if is_market_open("KR") and kis_equities_weekend_suppress_window_kst():
            print("💤 [주말 점검] 국장 매매 엔진 — 증권사 API 점검 구간으로 KIS 호출을 생략합니다.")
        else:
            print("💤 국장은 현재 휴장 상태입니다.")

    if is_market_open("US") and not kis_equities_weekend_suppress_window_kst():
        print("▶️ [🇺🇸 미장] 매매 엔진 시작...")
        us_cash = float(get_us_cash_real(kis_api.broker_us) or 0.0)
        us_bal = ensure_dict(get_us_positions_with_retry())
        out2 = safe_get(us_bal, 'output2', {})
        # =====================================================================
        # 🔥 [핵심 수술] KIS 야간 API 예수금 0원 증발 버그 치료 (GUI 로직 이식)
        # =====================================================================
        if us_cash <= 0.0 and out2:
            try:
                # API가 0원이라고 뻥을 쳐도, 잔고표(output2)를 뒤져서 진짜 외화예수금(frcr_dncl_amt_2)을 찾아냄
                if isinstance(out2, list) and len(out2) > 0:
                    fallback_cash = out2[0].get("frcr_dncl_amt_2", out2[0].get("frcr_buy_amt_smtl", 0.0))
                    us_cash = float(fallback_cash)
                elif isinstance(out2, dict):
                    fallback_cash = out2.get("frcr_dncl_amt_2", out2.get("frcr_buy_amt_smtl", 0.0))
                    us_cash = float(fallback_cash)
            except Exception as e:
                print(f"⚠️ 야간 예수금 복구 중 에러 발생: {e}")

        # 진짜 예수금 + 주식 평가금 = 진짜 총평가금 완료!
        # [수리] output2에서 주식평가금이 안 잡힐 경우를 대비해 output1(보유종목) 합산 로직 보강
        us_output1 = ensure_list(us_bal.get('output1', []))
        
        # 1. 일단 output2에서 주식 평가금 시도
        if isinstance(out2, list) and out2:
            us_stock_value = _to_float(out2[0].get('ovrs_stck_evlu_amt', 0))
        elif isinstance(out2, dict):
            us_stock_value = _to_float(out2.get('ovrs_stck_evlu_amt', 0))
        else:
            us_stock_value = 0.0

        # 2. 만약 output2에 주식 평가금이 0이라면, output1(보유종목)을 직접 다 더해서 수동 계산 (안전장치)
        if us_stock_value <= 0 and us_output1:
            manual_stock_eval = 0.0
            for s in us_output1:
                # frcr_evlu_amt2: 외화평가금액 (USD)
                val = _to_float(s.get('frcr_evlu_amt2', 0))
                if val <= 0:
                    # 외화평가금액이 없으면 (현재가 * 수량)으로 직접 계산
                    price = _to_float(s.get('ovrs_now_prc2', 0))
                    qty = _to_float(s.get('ovrs_cblc_qty', s.get('hldg_qty', 0)))
                    val = price * qty
                manual_stock_eval += val
            
            if manual_stock_eval > 0:
                print(f"  🔍 [잔고 보정] output2에 평가금 누락 감지 -> 보유종목 직접 합산: ${manual_stock_eval:.2f}")
                us_stock_value = manual_stock_eval

        total_us_equity = us_cash + us_stock_value
        print(f"  💰 [미장 자산 최종] 총자산: ${total_us_equity:.2f} (현금: ${us_cash:.2f} + 주식: ${us_stock_value:.2f})")

        state["circuit_aux_last_usd_total"] = float(total_us_equity)
        save_state(STATE_PATH, state)
        
        # ✅ [버그 수정] us_output1 정의 및 수량이 0보다 큰 종목만 held_us에 포함시킵니다.
        held_us = []
        for s in us_output1:
            qty = _to_float(s.get('ovrs_cblc_qty', s.get('ccld_qty_smtl1', s.get('hldg_qty', 0))))
            if qty > 0.0001:
                code = normalize_ticker(s.get('ovrs_pdno', s.get('pdno', '')))
                if code: held_us.append(code)

        # 디버깅: 보유 종목이 인식되었는지 확인
        print(f"  🔍 [US 잔고 데이터] 인식된 종목 수: {len(held_us)}개 / 리스트: {held_us}")
        if not held_us and 'msg1' in us_bal:
             print(f"  ⚠️ [US API 메시지] {us_bal.get('msg1')}")

        # 매도는 MDD와 무관하게 항상 실행 (손실 방어)
        sell_candidates = [
            code for code in held_us
            if code in state.get("positions", {}) and code not in CORE_ASSETS
        ]
        positions_count = len(sell_candidates)
        
        print(f"  🔍 [미장 매도 루프] 매도 대상 포지션 {positions_count}개 손익 체크 시작... (CORE_ASSETS 제외됨)")
        if positions_count == 0:
            print(f"  ✅ [미장 매도 루프] 매도할 종목 없음 (완료)")
        else:
            # 🗄️ OHLCV 일괄 캐싱 (yfinance)
            us_sell_tickers = sell_candidates
            prefetch_ohlcv(us_sell_tickers, market="US")
            
        for stock in us_output1:
            t_raw = stock.get('ovrs_pdno', stock.get('pdno', ''))
            t = normalize_ticker(t_raw)
            if not t:
                continue
            qty_holding = _to_float(stock.get('ovrs_cblc_qty', stock.get('ccld_qty_smtl1', stock.get('hldg_qty', 0))), 0.0)
            if qty_holding <= 0:
                 continue

            if t in CORE_ASSETS:
                print(f"  ⏭️  [{t}] CORE_ASSETS(True) - 스킵")
                continue
            if t not in state.get("positions", {}):
                avg_p = _to_float(stock.get('ovrs_avg_unpr', stock.get('ovrs_avg_pric', stock.get('ovrs_now_prc2', 0))), 0.0)
                if avg_p <= 0:
                    avg_p = _to_float(stock.get('ovrs_now_prc2', 0), 0.0)
                if avg_p > 0:
                    payload = {
                        'buy_p': float(avg_p),
                        'sl_p': float(avg_p * 0.9),
                        'max_p': float(avg_p),
                        'tier': '자동등록(보유종목)',
                        'buy_time': time.time(),
                        'buy_date': datetime.now().isoformat()
                    }
                    state.setdefault("positions", {})[t] = payload
                    save_state(STATE_PATH, state)
                    print(f"  🚨 [{t}] positions 미조회 → 즉시 자동등록 (buy_p=${avg_p:,.2f}, sl_p=${avg_p*0.9:,.2f})")
                else:
                    print(f"  ⏭️  [{t}] positions 미조회 + 평단/현재가 없음 - 스킵")
                    continue
            print(f"  🔍 [{t}] 매도 루프 진입 (장부 확인 완료, max_p 갱신 체크)")
            try:
                ohlcv = get_cached_ohlcv(t)
                
                if not ohlcv or not isinstance(ohlcv, list) or not ohlcv[-1] or 'c' not in ohlcv[-1]:
                    print(f"  ❌ [US 매도 루프 예외] {t}: OHLCV 데이터 또는 종가(c) 정보 부족. 건너뜁니다.")
                    continue

                pos_info = state.get("positions", {}).get(t, {})
                
                # 🔄 [완전 동기화] GUI가 장부에 공유한 최신 가격을 최우선으로 사용
                gui_p = pos_info.get('curr_p')
                if gui_p and float(gui_p) > 0:
                    curr_p = float(gui_p)
                else:
                    curr_p = float(ohlcv[-1]['c'])
                    try:
                        _price_resp = kis_api.broker_us.fetch_price(t)
                        if _price_resp and _price_resp.get('rt_cd') == '0':
                            _realtime_p = float(_price_resp.get('output', {}).get('last', 0))
                            if _realtime_p > 0: curr_p = _realtime_p
                    except Exception: pass

                buy_p = pos_info.get('buy_p', curr_p)
                max_p = pos_info.get('max_p', curr_p)
                hard_stop = float(pos_info.get('sl_p', buy_p * 0.9))
                profit_rate_now = ((curr_p - buy_p) / buy_p) * 100 if buy_p > 0 else 0.0

                # 📊 [상태 로그] 한눈에 보기
                us_name = get_us_company_name(t)
                chandelier_p = get_final_exit_price(t, curr_p, pos_info, ohlcv)
                print(f"  📊 [US 보유] {us_name}({t}) | 현재가: ${curr_p:.2f} | 매수가: ${buy_p:.2f} | 최고가: ${max_p:.2f} | 매도선: ${chandelier_p:.2f} | 수익률: {profit_rate_now:+.2f}%")

                # 0%~+1% 구간은 매도 보류 (신규 매수 후 15분 동안)
                buy_time = pos_info.get('buy_time', 0)
                time_elapsed = time.time() - buy_time if buy_time else 900
                if 0 <= profit_rate_now < 1.0 and time_elapsed < 900:
                    print(f"  ⏭️ {t}: 신규 매수 보호 구간 ({int(900-time_elapsed)}초 남음)")
                    continue

                reason = ""
                is_exit = False
                
                # 1. 보유 시간 계산
                now = datetime.now()
                now_str = now.strftime('%Y-%m-%d %H:%M:%S')
                buy_date_str = pos_info.get('buy_date')
                buy_time_ts = pos_info.get('buy_time')
                hours_held = 0
                buy_time_log = "알 수 없음"

                if buy_date_str:
                    try:
                        buy_datetime = datetime.fromisoformat(buy_date_str)
                        hours_held = (now - buy_datetime).total_seconds() / 3600
                        buy_time_log = buy_datetime.strftime('%Y-%m-%d %H:%M:%S')
                    except: pass

                if hours_held == 0 and buy_time_ts:
                    hours_held = (time.time() - buy_time_ts) / 3600
                    buy_time_log = datetime.fromtimestamp(buy_time_ts).strftime('%Y-%m-%d %H:%M:%S')

                print(f"  📊 [{now_str}] {t} 상태 체크")
                print(f"     ⏱️ 매수일시: {buy_time_log} ➔ 보유시간: {hours_held:.1f}시간")

                # 🛑 [매도 로직 1] 타임스탑 (7일(168시간) 경과 & 수익률 10% 미만)
                if hours_held >= 168:
                    if profit_rate_now < 10.0:
                        is_exit = True
                        reason = f"타임 스탑 발동 (보유 {hours_held:.1f}시간 / 수익률 {profit_rate_now:+.2f}%)"
                        print(f"  ⏰ [타임스탑 발동] {t} - 장기 보유 및 모멘텀 상실. 강제 청산!")
                    else:
                        print(f"     ✅ {hours_held:.1f}시간 경과했으나 수익률 양호({profit_rate_now:+.2f}%) ➔ 홀딩 유지")

                # 🛑 [매도 로직 2] 하드스탑 (손실 구간 방어)
                if not is_exit and profit_rate_now < 0:
                    print(f"  ⚠️  [{t}] 손실 구간: 수익률 {profit_rate_now:.2f}% (현재가: ${curr_p:.2f} / 손절가: ${hard_stop:.2f})")
                    if curr_p <= hard_stop:
                        is_exit = True
                        reason = "하드스탑 이탈 (손실구간 방어)"
                        print(f"  🔴 [하드스탑 발동] {t} - 현재가: ${curr_p:.2f} <= 손절가: ${hard_stop:.2f}. 강제 청산!")

                # 🛑 [매도 로직 3] 샹들리에 엑싯 (이익 보존 및 추세 종료)
                if not is_exit and profit_rate_now >= 0:
                    is_exit, reason_chandelier = check_pro_exit(t, curr_p, pos_info, ohlcv)
                    if is_exit:
                        reason = reason_chandelier

                # 🎯 실제 매도 주문 실행
                if is_exit:
                    # ✅ [핵심 버그 수정] 엉뚱하게 다시 구하지 말고 맨 위에서 구한 정확한 수량 재사용!
                    qty = qty_holding 
                    if qty <= 0:
                        print(f"  ❌ {t} 매도 오류: 수량이 0으로 인식됨.")
                        continue
                    
                    # 시장가 매도 (98% 지정가 = 즉시 체결 + 가격 보호)
                    sell_price = round(curr_p * 0.98, 2)
                    
                    # 최대 3회 재시도
                    retry_count = 0
                    max_retries = 3
                    resp = None
                    
                    while retry_count < max_retries:
                        resp = execute_us_order_direct(kis_api.broker_us, "sell", t, qty, sell_price)
                        if resp.get('rt_cd') == '0':
                            break
                        retry_count += 1
                        if retry_count < max_retries:
                            print(f"  ⚠️ {us_name}({t}) 매도 실패 (#{retry_count}): {resp.get('msg1', 'API 오류')} → 재시도")
                            time.sleep(1)  # 1초 대기 후 재시도
                    
                    if resp and resp.get('rt_cd') == '0':
                        profit_rate = ((sell_price - buy_p) / buy_p) * 100 if buy_p > 0 else 0.0
                        stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})
                        if profit_rate > 0:
                            stats["wins"] = int(stats.get("wins", 0)) + 1
                        else:
                            stats["losses"] += 1
                        stats["total_profit"] = float(stats.get("total_profit", 0.0) or 0.0) + float(profit_rate)
                        _record_trade_event("US", t, "SELL", qty, price=sell_price, profit_rate=profit_rate, reason=reason)
                        print(f"  ✅ [미장 매도 체결] {us_name}({t}) | 수익률: {profit_rate:+.2f}% | 사유: {reason}")
                        send_telegram(f"🚨 [미장 추세종료 매도] {t}({us_name})\n사유: {reason}\n최종 수익률: {profit_rate:.2f}%")
                        del state["positions"][t]
                        set_cooldown(state, t)
                        set_ticker_cooldown_after_sell(state, t, reason)
                        save_state(STATE_PATH, state)
                    else:
                        print(f"  ❌ {us_name}({t}) 매도 최종 실패 ({retry_count}회 시도): {resp.get('msg1', 'API 오류') if resp else '응답 없음'}")
            except Exception as e:
                print(f"  ❌ [US 매도 루프 예외] {t}: {e}")
                traceback.print_exc()
                continue
                
        # 매수는 MDD → Phase4 거시 체크 후에만 실행
        if not check_mdd_break("US", total_us_equity, state, STATE_PATH):
            print("  -> 🚨 미장 MDD 브레이크 작동 중. 신규 매수 중단.")
        elif macro_mult <= 0:
            print(f"  -> 🚨 미장 Phase4 거시 방어막: 신규 매수 중단. ({_macro_snap.get('reason', '')})")
        elif in_account_circuit_cooldown(state):
            print("  -> 🚨 미장 Phase5 계좌 서킷 쿨다운 — 신규 매수 중단.")
        else:
            # ⏳ [핵심] 미장 매수: NYSE 정규장 마감(16:00 ET) 직전 N분만 (기본 30분 → 15:30~15:59)
            now_ny = datetime.now(pytz.timezone("US/Eastern"))
            _us_close = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
            _us_buy_start = _us_close - timedelta(minutes=BUY_WINDOW_MINUTES_BEFORE_CLOSE)
            is_us_buy_time = _us_buy_start <= now_ny < _us_close

            if not is_us_buy_time:
                print(
                    f"  ⏳ [US 매수 대기] 장 마감 {BUY_WINDOW_MINUTES_BEFORE_CLOSE}분 전 구간만 매수 "
                    f"({_us_buy_start.strftime('%H:%M')}~{_us_close.strftime('%H:%M')} ET, "
                    f"현재 {now_ny.strftime('%H:%M')})"
                )
            else:
                # 지수 급락 체크
                us_index_change = get_market_index_change("US")
                print(f"  📊 [S&P500 지수] 변화율: {us_index_change:+.2f}% 날씨는 {weather['US']}")
                if us_index_change <= INDEX_CRASH_US:
                    print(f"  🚫 [US 매수 중단] S&P500 {us_index_change:+.2f}% 급락 (기준: {INDEX_CRASH_US}%)")
                elif weather['US'] == "🌧️ BEAR":
                    print(f"  🛑 [US 매수 중단] 현재 미장 날씨는 {weather['US']} 입니다. (현금 관망)")
                else:
                    if weather['US'] != "🌧️ BEAR":
                        # 2) 미장 타겟: S&P100 시총 + Ndx100\\S&P100 상위 50 (총 150, ``us_universe_cache.json``)
                        night_targets = get_top_market_cap_tickers(150)
                        total_us = len(night_targets)
                        print(f"  -> 🇺🇸 미장 유니버스(S&P100+Ndx50) {total_us}개 정밀 분석 시작!")
                        
                        for idx, t in enumerate(night_targets, 1):
                            try:
                                us_name = get_us_company_name(t)  # 종목명 미리 조회
                                if in_ticker_cooldown(state, t):
                                    print(
                                        f"  ⏭️ {us_name}({t}): 매도 후 쿨다운(톱날 방지) 만료 "
                                        f"{ticker_cooldown_human(state, t)} 이전 (패스)"
                                    )
                                    continue
                                # 핵심자산: cooldown 및 보유 여부 무시
                                if t in CORE_ASSETS:
                                    pass
                                else:
                                    if in_cooldown(state, t):
                                        print(f"  ⏭️ {us_name}({t}): 쿨다운 중 (패스)")
                                        continue
                                    if t in held_us:
                                        print(f"  ⏭️ {us_name}({t}): 이미 보유중 (패스)")
                                        continue
                                    sector_ok_us, sector_msg_us = allow_us_sector_entry(
                                        t,
                                        state.get("positions", {}),
                                        MAX_POSITIONS_US,
                                        normalize_ticker,
                                    )
                                    if not sector_ok_us:
                                        print(f"  ⏭️ {us_name}({t}): {sector_msg_us} (패스)")
                                        continue

                                # 🛡️ [수술 1] 개별 종목 ADX 폭발 시 하락장 무시 및 비중 상향 로직
                                ohlcv = get_ohlcv_yfinance(t)
                                if not ohlcv:
                                    print(f"  ⏭️ {us_name}({t}): OHLCV 데이터 부족 (패스)")
                                    continue
                                
                                try:
                                    if len(ohlcv) >= 15:
                                        df = pd.DataFrame(ohlcv)
                                        adx_indicator = ADXIndicator(
                                            high=df['h'], low=df['l'], close=df['c'], window=14
                                        )
                                        adx_val = adx_indicator.adx().iloc[-1]
                                        
                                        if adx_val >= 25:
                                            ratio, t_name = 0.40, "독고다이-ADX폭발"
                                        else:
                                            if weather['US'] == "☀️ BULL":
                                                if idx <= 50:
                                                    ratio, t_name = 0.40, "1티어(S&P100-우량)-불장"
                                                elif idx <= 100:
                                                    ratio, t_name = 0.20, "2티어(S&P100-성장)-불장"
                                                else:
                                                    ratio, t_name = 0.20, "3티어(Ndx100-특수)-불장"
                                            elif weather['US'] == "☁️ SIDEWAYS":
                                                if idx <= 50:
                                                    ratio, t_name = 0.30, "1티어(S&P100-우량)-횡보"
                                                elif idx <= 100:
                                                    ratio, t_name = 0.15, "2티어(S&P100-성장)-횡보"
                                                else:
                                                    ratio, t_name = 0.15, "3티어(Ndx100-특수)-횡보"
                                            else:
                                                if weather['US'] == "🌧️ BEAR": continue 
                                                ratio, t_name = 0.10, "방어-비중축소"
                                    else:
                                        if weather['US'] == "🌧️ BEAR": continue
                                        ratio, t_name = 0.10, "방어-비중축소"
                                except Exception:
                                    if weather['US'] == "🌧️ BEAR": continue
                                    ratio, t_name = 0.10, "방어-비중축소"

                                # 🛡️ [수술 4] 시드 확장 대비 동적 비율 캡(Dynamic Cap) 로직
                                max_allowed_ratio = 1.0 / max(1, MAX_POSITIONS_US)
                                ratio = min(ratio, max_allowed_ratio)

                                target_budget = total_us_equity * ratio * macro_mult
                                us_min_budget = 50.0  # 최소 주문금액
                                
                                # 🧹 [수술 2] 예수금 영끌(Sweep) 및 철통 방어 로직
                                if target_budget < us_min_budget:
                                    continue
                                if us_cash < us_min_budget:
                                    continue
                                if us_cash < target_budget:
                                    print(f"  🧹 [미장 영끌 발동] {us_name}({t}): 예산(${target_budget:.2f}) 부족. 지갑에 남은 전액(${us_cash:.2f}) 풀매수 장전!")
                                    target_budget = us_cash

                                is_buy, sl_p, s_name = calculate_pro_signals(ohlcv, weather['US'], t, us_name, idx, total_us)
                                if not is_buy:
                                    continue
                                if not can_open_new(t, state, max_positions=MAX_POSITIONS_US):
                                    print(f"  ⏭️ {us_name}({t}): 포지션 개수 초과 ({MAX_POSITIONS_US}개) (패스)")
                                    continue

                                curr_p = float(ohlcv[-1]['c'])
                                qty = int(target_budget / curr_p) if curr_p > 0 else 0
                                if qty > 0:
                                    # Phase 3: 모든 필터 통과 후 "매수 직전" AI 휩쏘 게이트
                                    if AI_FALSE_BREAKOUT_ENABLED:
                                        candles_15m = get_recent_15m_ohlcv(t, market="US", count=10)
                                        orderbook = get_orderbook_summary_from_broker(kis_api.broker_us, t)
                                        ai_eval = evaluate_false_breakout_filter(
                                            ticker=t,
                                            candles_15m_10=candles_15m,
                                            orderbook=orderbook,
                                            threshold=AI_FALSE_BREAKOUT_THRESHOLD,
                                            use_ai=True,
                                            ai_provider=AI_FALSE_BREAKOUT_PROVIDER,
                                            config=config,
                                        )
                                        if ai_eval.get("blocked"):
                                            print(
                                                f"  ⏭️ {us_name}({t}): [AI FILTER] 가짜돌파 {ai_eval.get('false_breakout_prob')}% "
                                                f"> {AI_FALSE_BREAKOUT_THRESHOLD}% ({ai_eval.get('provider')}) - 패스"
                                            )
                                            continue

                                    # 시장가 매수 (Phase2 TWAP: 대액 시 USD 분할, 슬라이스마다 101% 지정가)
                                    us_box = [float(us_cash)]
                                    _execute_us_market_buy_twap(
                                        t,
                                        us_name,
                                        float(target_budget),
                                        curr_p,
                                        sl_p,
                                        t_name,
                                        s_name,
                                        state,
                                        us_box,
                                    )
                                    us_cash = float(us_box[0])
                            except Exception as e:
                                print(f"  ❌ [US BUY 예외] {t}: {type(e).__name__}: {e}")
                                traceback.print_exc()
                                continue        
    else:
        if is_market_open("US") and kis_equities_weekend_suppress_window_kst():
            print("💤 [주말 점검] 미장 매매 엔진 — 증권사 API 점검 구간으로 KIS 호출을 생략합니다.")
        else:
            print("💤 미장은 현재 휴장 상태입니다.")

    if is_market_open("COIN"):
        coin_weather = weather.get('COIN', '☁️ SIDEWAYS')
        print("▶️ [🪙 코인] 매매 엔진 시작...")
        balances = upbit_api.upbit.get_balances() or []
        krw_row = next((b for b in balances if str(b.get("currency", "")).upper() == "KRW"), None) or {}
        krw_on_book = _to_float(krw_row.get("balance", 0), 0.0)
        krw_bal = _upbit_krw_spendable(balances)
        if krw_on_book > krw_bal + 1.0:
            print(
                f"  💡 [코인] KRW 장부 {krw_on_book:,.0f}원 중 "
                f"{krw_on_book - krw_bal:,.0f}원은 locked(미체결·출금대기 등) — 주문가능 {krw_bal:,.0f}원"
            )
        held_coins = [
            f"KRW-{b['currency']}"
            for b in balances
            if b.get('currency') not in ['KRW', 'VTHO']
            and coin_qty_counts_for_position(b.get('balance', 0))
            and float(_to_float(b.get('avg_buy_price', 0))) > 0
        ]

        total_coin_equity = float(krw_on_book)
        for b in balances:
            if b.get('currency') not in ['KRW', 'VTHO']:
                curr_p = pyupbit.get_current_price(f"KRW-{b['currency']}")
                if curr_p:
                    total_coin_equity += float(_to_float(b.get('balance', 0))) * float(curr_p)

        state["circuit_aux_last_coin_krw"] = float(total_coin_equity)
        save_state(STATE_PATH, state)

        # 매도는 MDD와 무관하게 항상 실행 (손실 방어)
        positions_count = len(
            [
                b
                for b in balances
                if f"KRW-{b.get('currency')}" in state.get("positions", {})
                and b.get('currency') not in ['KRW', 'VTHO']
                and coin_qty_counts_for_position(b.get('balance', 0))
            ]
        )
        print(f"  🔍 [코인 매도 루프] 보유 포지션 {positions_count}개 손익 체크 시작...")
        if positions_count == 0:
            print(f"  ✅ [코인 매도 루프] 매도할 종목 없음 (완료)")
        for b in balances:
            if b.get('currency') in ['KRW', 'VTHO']:
                continue
            t = f"KRW-{b['currency']}"
            
            # 🔥 [추가] 코어 자산은 매도 체크 자체를 패스
            if t in CORE_ASSETS:
                print(f"  💎 {t}: 코어 자산입니다. 매도 루프를 건너뜁니다.")
                continue
            is_exit = False

            if t not in state.get("positions", {}):
                print(f"  ⏭️ {t}: 장부에 없음 (패스)")
                continue
            qty = float(_to_float(b.get('balance', 0)))
            if not coin_qty_counts_for_position(qty):
                print(f"  ⏭️ {t}: 수량 너무 적음 ({qty}) (패스)")
                continue
            curr_p = pyupbit.get_current_price(t)
            if not curr_p:
                print(f"  ⏭️ {t}: 현재가 조회 실패 (패스)")
                continue
            df_upbit = pyupbit.get_ohlcv(t, interval="day", count=250)
            if df_upbit is None or len(df_upbit) < 20:
                # OHLCV 실패 시 현재가로만 손절 체크
                print(f"  ⚠️  [{t}] OHLCV 데이터 부족, 현재가로 손절만 체크...")
                pos_info = state.get("positions", {}).get(t, {})
                buy_p = pos_info.get('buy_p', curr_p)
                sl_p = float(pos_info.get('sl_p', buy_p * 0.9))
                profit_rate_now = ((curr_p - buy_p) / buy_p) * 100 if buy_p > 0 else 0.0
                
                # max_p 갱신 (OHLCV 실패 시에도)
                old_max_p = pos_info.get('max_p', buy_p)
                pos_info['max_p'] = max(old_max_p, curr_p)
                if pos_info['max_p'] > old_max_p:
                    print(f"     📈 [{t}] max_p 업데이트: {old_max_p:,.0f} → {pos_info['max_p']:,.0f}")
                state.setdefault("positions", {})[t] = pos_info
                save_state(STATE_PATH, state)
                
                print(f"     📊 {t}: 현재가 {curr_p:,.0f}원 / 손절가 {sl_p:,.0f}원 / 수익률 {profit_rate_now:+.2f}%")
                
            ohlcv = [{'o': row['open'], 'h': row['high'], 'l': row['low'], 'c': row['close'], 'v': row['volume']} for _, row in df_upbit.iterrows()]
            pos_info = state.get("positions", {}).get(t, {})
            
            # 🔄 [완전 동기화] GUI가 장부에 공유한 최신 가격을 최우선으로 사용
            gui_p = pos_info.get('curr_p')
            if gui_p and float(gui_p) > 0:
                curr_p = float(gui_p)
            # else: curr_p는 이미 위에서 pyupbit.get_current_price로 가져옴
            buy_p = pos_info.get('buy_p', curr_p)
            max_p = pos_info.get('max_p', curr_p)
            profit_rate_now = ((curr_p - buy_p) / buy_p) * 100 if buy_p > 0 else 0.0
            hard_stop = float(pos_info.get('sl_p', buy_p * 0.9))
            
            # 샹들리에 및 손절선 계산
            if len(ohlcv) < 20:
                print(f"  ⚠️  [{t}] OHLCV 데이터 부족, 샹들리에 대신 기본 손절선 방어 가동")
                chandelier_p = hard_stop
                if curr_p > max_p:
                    pos_info['max_p'] = curr_p
                    state.setdefault("positions", {})[t] = pos_info
                    save_state(STATE_PATH, state)
            else:
                chandelier_p = get_final_exit_price(t, curr_p, pos_info, ohlcv)
            
            # 🔧 [엽전주 버그 수정]
            curr_fmt = f"{curr_p:,.4f}" if curr_p < 100 else f"{curr_p:,.0f}"
            buy_fmt = f"{buy_p:,.4f}" if buy_p < 100 else f"{buy_p:,.0f}"
            max_fmt = f"{max_p:,.4f}" if max_p < 100 else f"{max_p:,.0f}"
            chan_fmt = f"{chandelier_p:,.4f}" if chandelier_p < 100 else f"{chandelier_p:,.0f}"
            hard_fmt = f"{hard_stop:,.4f}" if hard_stop < 100 else f"{hard_stop:,.0f}"
            
            # 📊 [상태 로그] 한눈에 보기
            print(f"  📊 [COIN 보유] {t} | 현재가: {curr_fmt}원 | 매수가: {buy_fmt}원 | 최고가: {max_fmt}원 | 매도선: {chan_fmt}원 | 수익률: {profit_rate_now:+.2f}%")

            # 손절가 체크 로그
            if profit_rate_now < 0:
                print(f"  ⚠️  [{t}] 손실 구간: 수익률 {profit_rate_now:.2f}% (현재가: {curr_fmt} / 손절가: {hard_fmt})")
                if curr_p <= hard_stop:
                    print(f"     ➜ 손절 체크: 현재가 {curr_fmt} ≤ 손절가 {hard_fmt} = 🔴 매도 신호!")

            # 0%~+1% 구간은 매도 보류 (신규 매수 후 15분 동안)
            buy_time = pos_info.get('buy_time', 0)
            time_elapsed = time.time() - buy_time if buy_time else 900
            if 0 <= profit_rate_now < 1.0 and time_elapsed < 900:
                print(f"  ⏭️ {t}: 신규 매수 보호 구간 ({int(900-time_elapsed)}초 남음)")
                continue

            # 매도 결정 로직 (우선순위: 타임스탑 > 하드스탑 > 샹들리에)
            reason = ""

            # 1. 현재 시간 및 매수 시간 추출
            now = datetime.now()
            now_str = now.strftime('%Y-%m-%d %H:%M:%S') # 현재(매도 체크) 시간

            buy_date_str = pos_info.get('buy_date')
            buy_time_ts = pos_info.get('buy_time')

            hours_held = 0
            buy_time_log = "알 수 없음"

            # 2. 보유 시간 계산 (buy_date 우선, 없으면 buy_time 사용)
            if buy_date_str:
                try:
                    buy_datetime = datetime.fromisoformat(buy_date_str)
                    hours_held = (now - buy_datetime).total_seconds() / 3600
                    buy_time_log = buy_datetime.strftime('%Y-%m-%d %H:%M:%S')
                except:
                    pass

            # buy_date가 없거나 파싱에 실패했는데 buy_time은 있는 경우 ("278470" 케이스 해결)
            if hours_held == 0 and buy_time_ts:
                hours_held = (time.time() - buy_time_ts) / 3600
                # 타임스탬프를 사람이 읽을 수 있는 날짜 포맷으로 변환
                buy_time_log = datetime.fromtimestamp(buy_time_ts).strftime('%Y-%m-%d %H:%M:%S')

            # 📊 상태 로그 출력 (매수 날짜 및 보유 시간 포함)
            print(f"📊 [{now_str}] {t} 상태 체크")
            print(f"   ⏱️ 매수일시: {buy_time_log} ➔ 보유시간: {hours_held:.1f}시간")

            # 72시간(3일) 경과 & 수익률 10% 미만일 경우
            if hours_held >= 72:
                if profit_rate_now < 10.0:
                    is_exit = True
                    reason = f"타임 스탑 발동 (보유 {hours_held:.1f}시간 / 수익률 {profit_rate_now:+.2f}%)"
                else:
                    # 수익률이 10% 이상이라 홀딩할 때의 로그
                    print(f"   ✅ {hours_held:.1f}시간 경과했으나 수익률 양호({profit_rate_now:+.2f}%) ➔ 홀딩 유지")
                    
            # 2. 하드스탑 체크 (타임스탑이 발동되지 않았을 때만)
            if not is_exit and profit_rate_now < 0:
                if curr_p <= hard_stop:
                    is_exit = True
                    reason = "하드스탑 이탈 (손실구간 방어)"
                    print(f"🔴 [하드스탑 발동] {t} - 현재가: {curr_p:,.0f}원 <= 손절가: {hard_stop:,.0f}원. 강제 청산! (is_exit={is_exit})")

            # 3. 샹들리에 엑싯 체크 (타임스탑, 하드스탑 모두 발동되지 않았을 때만)
            if not is_exit and profit_rate_now >= 0: # 수익 구간일 때만 샹들리에 검사
                is_exit, reason_chandelier = check_pro_exit(t, curr_p, pos_info, ohlcv)
                if is_exit: # 샹들리에가 True를 반환하면 reason 업데이트
                    reason = reason_chandelier
            
            if is_exit: # 여기서 실제 매도 주문이 나감
                # 최대 3회 재시도
                retry_count = 0
                max_retries = 3
                resp = None

                # 1. 매도 주문 실행 루프
                while retry_count < max_retries:
                    resp = upbit_api.upbit.sell_market_order(t, qty)
                    if resp:
                        break # 성공 시 while 루프 즉시 탈출 (정상)
                    
                    retry_count += 1
                    if retry_count < max_retries:
                        print(f"  ⚠️ {t} 매도 실패 (#{retry_count}): upbit API 오류 → 재시도")
                        time.sleep(0.5) # API 호출 제한(Rate Limit) 방지를 위해 약간 대기

                # 2. 루프 종료 후, 매도 성공 여부에 따라 장부 기록
                if resp: # 매도가 성공적으로 체결되었다면
                    profit_rate = ((curr_p - buy_p) / buy_p) * 100 if buy_p > 0 else 0.0
                    stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})
                    
                    if profit_rate > 0:
                        stats["wins"] = int(stats.get("wins", 0)) + 1
                    else:
                        stats["losses"] += 1
                        
                    stats["total_profit"] = float(stats.get("total_profit", 0.0) or 0.0) + float(profit_rate)
                    
                    _record_trade_event("COIN", t, "SELL", qty, price=curr_p, profit_rate=profit_rate, reason=reason)
                    
                    coin_name = get_coin_name(t)
                    print(f"  ✅ [코인 매도 체결] {t}({coin_name}) | 수익률: {profit_rate:+.2f}% | 사유: {reason}")
                    send_telegram(f"🚨 [코인 추세종료 매도] {t}({coin_name})\n사유: {reason}\n최종 수익률: {profit_rate:.2f}%")
                    # 장부 업데이트 및 저장
                    del state["positions"][t]
                    set_cooldown(state, t)
                    set_ticker_cooldown_after_sell(state, t, reason)
                    save_state(STATE_PATH, state)
                    
                else: # 3번 모두 실패했다면
                    print(f"  ❌ {t} 매도 최종 실패 ({retry_count}회 시도): upbit API 오류")

        # 매수는 MDD → Phase4 거시 체크 후에만 실행
        if not check_mdd_break("COIN", total_coin_equity, state, STATE_PATH):
            print("  -> 🚨 코인 MDD 브레이크 작동 중. 신규 매수 중단.")
        elif macro_mult <= 0:
            print(f"  -> 🚨 코인 Phase4 거시 방어막: 신규 매수 중단. ({_macro_snap.get('reason', '')})")
        elif in_account_circuit_cooldown(state):
            print("  -> 🚨 코인 Phase5 계좌 서킷 쿨다운 — 신규 매수 중단.")
        else:
            # ⏳ [핵심] 코인 매수: KST 09:00 일봉 전환 직전 N분(기본 30분 → 08:30~08:59, 업비트 24h 중 전략용 창)
            now_coin = datetime.now(pytz.timezone("Asia/Seoul"))
            _coin_close = now_coin.replace(hour=9, minute=0, second=0, microsecond=0)
            _coin_buy_start = _coin_close - timedelta(minutes=BUY_WINDOW_MINUTES_BEFORE_CLOSE)
            is_coin_buy_time = _coin_buy_start <= now_coin < _coin_close

            if not is_coin_buy_time:
                print(
                    f"  ⏳ [COIN 매수 대기] 일봉 기준점 직전 {BUY_WINDOW_MINUTES_BEFORE_CLOSE}분만 매수 "
                    f"({_coin_buy_start.strftime('%H:%M')}~{_coin_close.strftime('%H:%M')} KST, "
                    f"현재 {now_coin.strftime('%H:%M')})"
                )
            else:
                # 지수 급락 체크
                coin_index_change = get_market_index_change("COIN")
                print(f"  📊 [BTC 지수] 변화율: {coin_index_change:+.2f}% 날씨는 {coin_weather}")
                if coin_index_change <= INDEX_CRASH_COIN:
                    print(f"  🚫 [COIN 매수 중단] BTC {coin_index_change:+.2f}% 급락 (기준: {INDEX_CRASH_COIN}%)")
                elif coin_weather == "🌧️ BEAR":
                    print(f"  🛑 [COIN 매수 중단] 현재 코인 날씨는 {coin_weather} 입니다. (현금 관망)")   
                else:
                        if coin_weather == "🌧️ BEAR":
                            print("  ⏭️ 코인 시장이 베어 상태라 신규 매수 안함 (패스)")
                        else:
                            try:
                                markets = [m['market'] for m in requests.get("https://api.upbit.com/v1/market/all", timeout=10).json() if m.get('market', '').startswith("KRW-")]
                                tickers_data = requests.get("https://api.upbit.com/v1/ticker?markets=" + ",".join(markets), timeout=10).json()
                                scan_targets = [x['market'] for x in sorted(tickers_data, key=lambda x: x.get('acc_trade_price_24h', 0), reverse=True)[:20]]
                            except Exception:
                                scan_targets = list(CORE_COIN_ASSETS)

                            print(f"  -> 🪙 코인 실시간 수급 상위 {len(scan_targets)}개 정밀 분석 시작!")
                            for idx, t in enumerate(scan_targets, 1):
                                if in_ticker_cooldown(state, t):
                                    print(
                                        f"  ⏭️ {t}: 매도 후 쿨다운(톱날 방지) 만료 "
                                        f"{ticker_cooldown_human(state, t)} 이전 (패스)"
                                    )
                                    continue
                                if t not in CORE_ASSETS:
                                    if in_cooldown(state, t):
                                        print(f"  ⏭️ {t}: 쿨다운 중 (패스)")
                                        continue
                                    if not can_open_new(t, state, max_positions=MAX_POSITIONS_COIN):
                                        print(f"  ⏭️ {t}: 포지션 개수 초과 ({MAX_POSITIONS_COIN}개) (패스)")
                                        continue
                                else:
                                    print(f"  🔥 {t}: 코어 자산 분석 중... (제약 조건 무시)")        
                                df_upbit = pyupbit.get_ohlcv(t, interval="day", count=250)
                                if df_upbit is None or len(df_upbit) < 20:
                                    print(f"  ⏭️ {t}: OHLCV 데이터 부족 (패스)")
                                    continue
                                ohlcv = [{'o': row['open'], 'h': row['high'], 'l': row['low'], 'c': row['close'], 'v': row['volume']} for _, row in df_upbit.iterrows()]
                                
                                # 🛡️ [수술 1] 개별 종목 ADX 폭발 시 하락장 무시 및 비중 상향 로직
                                try:
                                    if len(ohlcv) >= 15:
                                        df = pd.DataFrame(ohlcv)
                                        adx_indicator = ADXIndicator(
                                            high=df['h'], low=df['l'], close=df['c'], window=14
                                        )
                                        adx_val = adx_indicator.adx().iloc[-1]
                                        
                                        if adx_val >= 25:
                                            ratio, t_name = 0.40, "독고다이-ADX폭발"
                                        else:
                                            if coin_weather == "☀️ BULL":
                                                ratio, t_name = 0.40, "스나이퍼-불장"
                                            elif coin_weather == "☁️ SIDEWAYS":
                                                ratio, t_name = 0.30, "스나이퍼-횡보"
                                            else:
                                                if coin_weather == "🌧️ BEAR": continue 
                                                ratio, t_name = 0.15, "스나이퍼-방어"
                                    else:
                                        if coin_weather == "🌧️ BEAR": continue
                                        ratio, t_name = 0.15, "스나이퍼-방어"
                                except Exception:
                                    if coin_weather == "🌧️ BEAR": continue
                                    ratio, t_name = 0.15, "스나이퍼-방어"

                                # 🛡️ [수술 4] 시드 확장 대비 동적 비율 캡(Dynamic Cap) 로직
                                max_allowed_ratio = 1.0 / max(1, MAX_POSITIONS_COIN)
                                ratio = min(ratio, max_allowed_ratio)

                                budget = total_coin_equity * ratio * macro_mult
                                coin_min_budget = UPBIT_COIN_MIN_ORDER_KRW  # 업비트 KRW 마켓 최소 주문

                                # 🧹 [수술 2] 예수금 영끌(Sweep) 및 철통 방어 로직
                                if budget < coin_min_budget:
                                    continue
                                if krw_bal < coin_min_budget:
                                    continue
                                if krw_bal < budget:
                                    print(f"  🧹 [코인 영끌 발동] {t}: 예산({int(budget):,}원) 부족. 지갑에 남은 전액({int(krw_bal):,}원) 풀매수 장전!")
                                    budget = krw_bal

                                is_buy, sl_p, s_name = calculate_pro_signals(ohlcv, coin_weather, t, get_coin_name(t), idx, len(scan_targets))
                                if not is_buy:
                                    continue
                                
                                if budget >= coin_min_budget:
                                    # Phase 3: 코인도 "매수 직전 마지막 게이트" 적용
                                    if AI_FALSE_BREAKOUT_ENABLED:
                                        candles_15m = get_recent_15m_ohlcv(t, market="COIN", count=10)
                                        orderbook = get_orderbook_summary_for_coin(t)
                                        ai_eval = evaluate_false_breakout_filter(
                                            ticker=t,
                                            candles_15m_10=candles_15m,
                                            orderbook=orderbook,
                                            threshold=AI_FALSE_BREAKOUT_THRESHOLD_COIN,
                                            use_ai=True,
                                            ai_provider=AI_FALSE_BREAKOUT_PROVIDER,
                                            config=config,
                                        )
                                        if ai_eval.get("blocked"):
                                            print(
                                                f"  ⏭️ {t}: [AI FILTER] 가짜돌파 {ai_eval.get('false_breakout_prob')}% "
                                                f"> {AI_FALSE_BREAKOUT_THRESHOLD_COIN}% ({ai_eval.get('provider')}) - 패스"
                                            )
                                            continue

                                    krw_box = [float(krw_bal)]
                                    _execute_coin_market_buy_twap(
                                        t, float(budget), sl_p, s_name, state, krw_box, held_coins
                                    )
                                    krw_bal = float(krw_box[0])
                                else:
                                    print(f"  ⏭️ {t}: 예수금 부족 (현재: {int(krw_bal):,}원 < 필요: 5,500원) (패스)")
    else:
        print("💤 코인은 점검 또는 데이터 조회 불가 상태입니다.")

    save_state(STATE_PATH, state)
    print("="*60)

# =====================================================================
# 7. 스케줄러 — ``schedule`` 패키지의 pending 잡을 백그라운드 스레드에서 소비
# =====================================================================
def run_continuously(interval=1):
    """
    ``schedule.run_pending()`` 를 무한 루프로 돌리는 **데몬 스레드**를 1회 기동한다.

    GUI 모드에서 ``run_trading_bot`` 은 QTimer(singleShot 체인)으로 **KST 분봉 정각**에 맞춰 호출되며,
    ``start_scanner_scheduler`` 가 등록한 **일 1회 스캐너** 같은 잡은 이 루프가 처리한다.
    중복 기동은 ``_schedule_loop_started`` 로 막는다.
    """
    global _schedule_loop_started
    if _schedule_loop_started:
        return

    class ScheduleThread(threading.Thread):
        @classmethod
        def run(cls):
            while True:
                schedule.run_pending()
                time.sleep(interval)
    
    continuous_thread = ScheduleThread()
    continuous_thread.daemon = True
    continuous_thread.start()
    _schedule_loop_started = True

def start_scanner_scheduler():
    """
    **국장·미장 스크리너**를 매 거래일 각 시장 **매수 가능시간 10분 전**에 돌리도록 등록한다.

    * 국장: ``screener.run_night_screener`` — 매일 **14:50 Asia/Seoul**
      (국장 매수 창 15:00 KST 의 10분 전).
    * 미장: ``us_screener.run_us_screener`` — 매일 **15:20 US/Eastern**
      (미장 매수 창 15:30 ET 의 10분 전, DST 자동 반영).
    * ``schedule`` 의 ``scanner`` 태그 잡만 지운 뒤 재등록해 중복을 방지한다.
    * 타임존 인자를 지원하지 않는 ``schedule`` 버전이면 로컬시간으로 폴백한다.
    * 마지막에 ``run_continuously()`` 를 호출해 pending 처리 스레드를 보장한다.
    """
    global _scanner_started

    def _run_kr_scanner_job():
        now_kst = datetime.now(pytz.timezone('Asia/Seoul')).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[KR 스캐너] 실행 시작: {now_kst} (국장 매수창 10분 전 — 14:50 KST)")
        try:
            screener.run_night_screener()
            print("[KR 스캐너] 실행 완료")
        except Exception as e:
            print(f"[KR 스캐너] 실행 실패: {e}")
            traceback.print_exc()

    def _run_us_scanner_job():
        now_et = datetime.now(pytz.timezone('US/Eastern')).strftime("%Y-%m-%d %H:%M %Z")
        print(f"[US 스캐너] 실행 시작: {now_et} (미장 매수창 10분 전 — 15:20 ET)")
        try:
            import us_screener
            us_screener.run_us_screener()
            print("[US 스캐너] 실행 완료")
        except Exception as e:
            print(f"[US 스캐너] 실행 실패: {e}")
            traceback.print_exc()

    # GUI 재진입/중복호출 대비: 스캐너 태그 스케줄만 정리 후 재등록
    schedule.clear("scanner")

    try:
        schedule.every().day.at("14:50", "Asia/Seoul").do(_run_kr_scanner_job).tag("scanner")
    except TypeError:
        print("⚠️ schedule timezone 인자 미지원 - 국장 스캐너를 로컬 14:50 으로 폴백 등록")
        schedule.every().day.at("14:50").do(_run_kr_scanner_job).tag("scanner")

    try:
        schedule.every().day.at("15:20", "US/Eastern").do(_run_us_scanner_job).tag("scanner")
    except TypeError:
        print("⚠️ schedule timezone 인자 미지원 - 미장 스캐너를 로컬 04:20 으로 폴백 등록")
        # DST 무시 단순 폴백: ET 15:20 ≒ KST 04:20(여름)/05:20(겨울). 여름 기준.
        schedule.every().day.at("04:20").do(_run_us_scanner_job).tag("scanner")

    scanner_jobs = [job for job in schedule.jobs if "scanner" in getattr(job, "tags", set())]
    print(f"[스캐너] 등록 완료: {len(scanner_jobs)}개 (KR 14:50 KST / US 15:20 ET)")
    for idx, job in enumerate(scanner_jobs, 1):
        print(f"  - scanner#{idx} next_run={job.next_run}")
    _scanner_started = True

    # GUI 모드에서도 schedule.run_pending()가 돌도록 보장
    run_continuously()

# =====================================================================
# 8. 메인 진입점 — ``python -m run_bot`` / ``python run_bot.py``
# =====================================================================

def __getattr__(name):
    """브로커/업비트 객체는 api 모듈에 있으나 run_bot.broker_kr 등 속성 접근을 유지."""
    if name in ("broker_kr", "broker_us", "KIS_TOKEN"):
        import api.kis_api as _kis
        return getattr(_kis, name)
    if name == "upbit":
        import api.upbit_api as _ub
        return _ub.upbit
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> None:
    """콘솔(헤드리스) 실행: 브로커 초기화, 장부 동기화, 스케줄·스캐너 가동."""
    print("=" * 50)
    print("🤖 V6.5 통합 자동매매 봇 (완전판)")
    print("=" * 50)

    print("[초기화] KIS 토큰 및 브로커 객체 설정...")
    refresh_brokers_if_needed()
    if kis_api.broker_kr is None:
        print("🚨 브로커 초기화 실패. 프로그램을 종료합니다.")
        sys.exit(1)
    print("[초기화] 완료.")

    held_kr = get_held_stocks_kr()
    held_us = get_held_stocks_us()
    held_coins = get_held_coins()

    state = load_state(STATE_PATH)

    if held_kr is not None and held_us is not None and held_coins is not None:
        sync_all_positions(state, held_kr, held_us, held_coins, STATE_PATH)
    else:
        failed_apis = []
        if held_kr is None:
            failed_apis.append("국장")
        if held_us is None:
            failed_apis.append("미장")
        if held_coins is None:
            failed_apis.append("코인")
        error_msg = f"실보유 조회 실패 ({', '.join(failed_apis)} API 오류)"
        print(f"  ⚠️ [장부 동기화 건너뜀] {error_msg} - 기존 장부 유지")

    heartbeat_report()
    run_trading_bot()

    schedule.every(4).hours.do(heartbeat_report)

    # 매매 사이클: KST 벽시계 :00 / :15 / :30 / :45 (기동 직후 위에서 1회 이미 실행됨)
    schedule.clear("trading")
    for minute_mark in (":00", ":15", ":30", ":45"):
        try:
            schedule.every().hour.at(minute_mark, "Asia/Seoul").do(run_trading_bot).tag("trading")
        except TypeError:
            schedule.every().hour.at(minute_mark).do(run_trading_bot).tag("trading")

    start_scanner_scheduler()

    run_continuously()
    print("\n✅ 모든 시스템이 정상적으로 가동되었습니다.")
    print("  [스케줄] 매매: 매시 KST :00 / :15 / :30 / :45 + 기동 직후 1회")

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
