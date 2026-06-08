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
    * ``bot_state.json`` — positions, cooldown, stats, Phase5 ``peak_total_equity``·``last_reset_week``·
      ``account_circuit_peak_reset_pending``·서킷 쿨다운 등(월요일 주차 MDD).
    * ``trade_history.json`` — 체결/이벤트 기록(락 사용).

주요 의존
    * ``api.kis_api`` / ``api.upbit_api`` — 브로커·주문.
    * ``execution.guard`` / ``execution.sync_positions`` — 장부·MDD·실보유 동기화.
    * ``execution.order_twap`` — 대액 시장가 분할 매수(TWAP).
    * ``strategy.rules`` — 진입/청산 시그널·OHLCV.
    * ``strategy.sector_lock`` / ``strategy.ai_filter`` / ``strategy.macro_guard`` — 섹터·휩쏘·거시.
    * ``strategy.hedge_universe`` — **하락장 헷지 티커 하드코딩 단일 출처** (grep: ``HEDGE_UNIVERSE``).

코드 맵
    * 상단 유틸 — 지수 변화율, 미장 유니버스(고베타 NDX+S&P RR 150, ``us_universe_cache.json``) 등.
    * ``# 0. 기본 설정`` 이후 — 경로, 로깅, config 전역, Phase2~5 플래그.
    * ``run_trading_bot()`` — 한 사이클(동기화 → 손절/익절 → 스크리너 → 신규 매수).
    * ``run_continuously`` / ``start_scanner_scheduler`` — schedule 루프·스캐너(매매는 매시 KST :00/:15/:30/:45).

관측성(로그) 정책 — 2026-04-22
    * **조용한 패스 금지(원칙):** 예산·예수금·최소주문·정수주 0·TWAP 미체결·조회 실패·장부 폴백은
      가능한 한 ``⏭️`` / ``⚠️`` / ``❌`` 접두와 **태그**(`[KR 예산 부족]`, `[US 매수 미체결]` 등)로 남긴다.
    * ``strategy.rules.calculate_pro_signals`` 는 실패 시 이미 상세 로그를 출력하므로, 호출부는
      **중복 없이** 금액·비중·macro 등 **호출부만 아는 맥락**을 덧붙인다.
    * ``services/account_read_facade`` — 주말/예외 시 **장부 폴백** 또는 빈 리스트 반환 시에도 한 줄 로그로 이유를 남긴다.
"""
import warnings
# pandas_market_calendars 내부 공지성 UserWarning이 stderr로 찍혀 텔레그램 경고 감시에 걸린다.
# 타임스탑/거래일 판별에는 영향이 없으므로 해당 경고만 선택적으로 무시한다.
warnings.filterwarnings(
    "ignore",
    message=r".*break_start.*break_end.*discontinued.*",
    category=UserWarning,
)
import time, json, schedule, pyupbit, requests, traceback, threading, sys, os
import pytz
from ta.trend import ADXIndicator
from pathlib import Path
from datetime import datetime, timedelta, time as dt_time
import yfinance as yf
import pandas as pd
import pandas_market_calendars as mcal
import concurrent.futures
from api.kis_parsers import (
    compute_us_stock_value_from_output,
    extract_held_kr_codes,
    extract_held_us_codes,
    parse_kr_cash_total,
    parse_kr_holdings_metrics,
    parse_us_cash_fallback,
    parse_us_holdings_metrics,
)
from execution.guard import (
    ACCOUNT_CIRCUIT_COOLDOWN_KEY,
    ACCOUNT_CIRCUIT_PEAK_RESET_PENDING_KEY,
    LAST_RESET_WEEK_KEY,
    PEAK_TOTAL_EQUITY_KEY,
    PHASE5_PENDING_LIQUIDATION_MARKETS_KEY,
    apply_phase5_share_anchor,
    apply_phase5_trailing_week_and_cooldown,
    can_open_new,
    check_mdd_break,
    get_phase5_peak_total_equity,
    get_phase5_share_anchor,
    in_account_circuit_cooldown,
    in_cooldown,
    in_market_circuit_cooldown,
    in_ticker_cooldown,
    load_state,
    save_state,
    set_account_circuit_cooldown,
    set_cooldown,
    set_market_circuit_cooldown,
    set_ticker_cooldown_after_sell,
    ticker_cooldown_human,
)
from execution.circuit_break import (
    evaluate_per_market_share_circuits,
    evaluate_total_account_circuit,
    estimate_usdkrw,
)
from execution.scale_out import (
    SCALE_OUT_ENTRY_ATR_MULT,
    SCALE_OUT_MIN_NOTIONAL_KRW,
    SCALE_OUT_PROFIT_PCT,
    SCALE_OUT_SECOND_ENTRY_ATR_MULT,
    compute_coin_scale_out_qty,
    compute_stock_scale_out_qty,
    coin_scale_out_min_notional_ok,
    notional_krw_kr_us,
    plan_coin_sell_chunks,
    position_scale_out_done,
    position_second_scale_out_done,
    post_partial_ledger,
    scale_out_price_target_hit,
    scale_out_second_trigger_ok,
    scale_out_trigger_ok,
    stock_scale_out_min_notional_ok,
    truncate_coin_qty,
)
from execution.sync_positions import sync_all_positions, _last_buy_price_from_trade_history
from execution.order_twap import plan_sell_qty_twap
from execution import balance_read as bal_read
from execution import idempotency as order_idem
from execution import ledger_apply as ledger_apply
from strategy.ai_filter import (
    evaluate_false_breakout_filter,
    summarize_ai_rationale,
)
from strategy.hedge_universe import (
    HEDGE_TICKERS_KR,
    HEDGE_TICKERS_US,
    HEDGE_TICKERS_COIN,
    format_hedge_universe_summary,
    hedge_tickers_for_market,
    is_coin_hedge_internal_ticker,
)
from strategy.entry_router import decide_entry_signals
from strategy.exit_router import decide_swing_exit, decide_v8_exit
from strategy.rules import (
    check_swing_profit_lock_trailing_exit,
    get_final_exit_price,
    get_swing_exit_display_price,
    get_swing_scale_out_target_price,
    get_swing_hard_stop_floor,
    get_v8_profit_lock_floor,
    reconcile_swing_position,
    register_swing_entry_risk_fields,
    swing_entry_sl_p,
    SWING_SCALE_OUT_R_MULT,
    get_ohlcv_yfinance,
    get_ohlcv_stooq,
    get_ohlcv_pykrx,
    get_ohlcv_realtime,
    get_ohlcv_kis_domestic_daily,
)
from strategy.market_hours import trading_hours_elapsed
from strategy.indicators import get_safe_atr
from services.account_snapshot import (
    resolve_display_current_price as _resolve_display_current_price,
)
from services import account_read_facade
from strategy.sector_lock import allow_kr_sector_entry, allow_us_sector_entry, seed_us_sector_cache
from strategy.macro_guard import (
    get_macro_guard_snapshot,
)
from strategy.alpha_sizing import (
    atr_pct_from_ohlcv,
    compute_market_portfolio_heat,
    portfolio_heat_blocks_entry,
    sort_targets_by_relative_strength,
    volatility_target_ratio,
)
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
            bt = coin_config.btc_benchmark_ticker()
            oc = coin_broker.fetch_ohlcv(bt, "day", 3)
            if oc and len(oc) >= 2:
                prev_close = float(oc[-2]["c"])
                curr_close = float(oc[-1]["c"])
                if prev_close > 0:
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
    미장 감시 유니버스 — ``us_screener`` 고베타·섹터 분산 모델 (최대 150).

    * NDX 우선(~90) + S&P 섹터 라운드로빈 잔여 슬롯
    * 배제: Utilities, Consumer Staples/Defensive, Real Estate, Basic Materials
    * ``us_universe_cache.json`` — tickers + GICS sectors, 24h TTL
    """
    import us_screener

    base_dir = Path(__file__).resolve().parent
    return us_screener.load_or_build_us_universe(
        limit=limit,
        force_refresh=force_refresh,
        cache_path=base_dir / US_UNIVERSE_CACHE_FILE,
        ttl_sec=US_UNIVERSE_CACHE_TTL_SEC,
    )

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

import logging

for _yn in ("yfinance", "urllib3"):
    logging.getLogger(_yn).setLevel(logging.ERROR)

from utils.telegram import (
    attach_telegram_error_alerts,
    configure_telegram,
    register_telegram_atexit,
    send_telegram,
)
from utils.helpers import (
    is_coin_ticker,
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
from utils.trade_sector import resolve_trade_sector
configure_kis_token_path(KIS_TOKEN_PATH)
configure_trade_history(TRADE_HISTORY_PATH, TRADE_HISTORY_LOCK)

with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

configure_telegram(config)
register_telegram_atexit()
attach_telegram_error_alerts()

from api import kis_api, upbit_api
from api import coin_broker, coin_config

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

# 하락장 헷지 티커 — 정의·한글명은 strategy/hedge_universe.py (grep: HEDGE_UNIVERSE)
# HEDGE_TICKERS_KR / HEDGE_TICKERS_US / HEDGE_TICKERS_COIN 는 위 모듈에서 re-export.

# Phase 3: AI False Breakout filter
AI_FALSE_BREAKOUT_ENABLED = bool(config.get("ai_false_breakout_enabled", True))
AI_FALSE_BREAKOUT_THRESHOLD = int(config.get("ai_false_breakout_threshold", 70))
AI_FALSE_BREAKOUT_THRESHOLD_COIN = int(config.get("ai_false_breakout_threshold_coin", 80))
AI_FALSE_BREAKOUT_PROVIDER = str(config.get("ai_false_breakout_provider", "gemini") or "gemini").strip().lower()

# Phase 5 / Dry-run: config.json — test_mode=true 시 주문 대신 로그·텔레그램만
TEST_MODE = bool(config.get("test_mode", False))
# Phase5 계좌 서킷 — 기본: 시장별 포트폴리오 비중 하한(합산 MDD는 레거시, 기본 OFF)
ACCOUNT_CIRCUIT_ENABLED = bool(config.get("account_circuit_enabled", True))
ACCOUNT_CIRCUIT_USE_TOTAL = bool(config.get("account_circuit_use_total", False))
ACCOUNT_CIRCUIT_MDD_PCT = float(config.get("account_circuit_mdd_pct", 15.0))
ACCOUNT_CIRCUIT_COOLDOWN_H = float(config.get("account_circuit_cooldown_hours", 24.0))
ACCOUNT_CIRCUIT_ANCHOR_MIN_RATIO = float(config.get("account_circuit_share_anchor_min_ratio", 0.5))
# KIS 잔고: ``on_trade``(기본)=매매·강제새로고침·입출금 시만 API / ``always``=기존처럼 자주 조회
KIS_BALANCE_SYNC_MODE = str(config.get("kis_balance_sync_mode", "on_trade")).strip().lower()


def _account_circuit_min_share_pct(market: str) -> float:
    """시장별 최소 포트폴리오 비중(%). 0 이하면 해당 시장 서킷 비활성."""
    mk = str(market or "").strip().upper()
    nested = config.get("account_circuit_min_share_pct")
    if isinstance(nested, dict) and mk in nested:
        try:
            return float(nested.get(mk, 0.0) or 0.0)
        except (TypeError, ValueError):
            pass
    defaults = {"KR": 8.0, "US": 8.0, "COIN": 5.0}
    key = f"account_circuit_min_share_{mk.lower()}_pct"
    try:
        return float(config.get(key, defaults.get(mk, 0.0)))
    except (TypeError, ValueError):
        return float(defaults.get(mk, 0.0))

# Phase 2: 대액 시장가 매수 TWAP (원화 500만 / USD 5000 초과 시 분할)
TWAP_ENABLED = bool(config.get("twap_enabled", True))
TWAP_KRW_THRESHOLD = float(config.get("twap_krw_threshold", 5_000_000))
TWAP_USD_THRESHOLD = float(config.get("twap_usd_threshold", 5000))
TWAP_SLICE_DELAY_SEC = float(config.get("twap_slice_delay_sec", 90))
# 매수: 장(또는 일봉 기준) 마감 직전 N분만 허용 (기본 30분). TWAP 분할 시 마감 직후 주문 방지
BUY_WINDOW_MINUTES_BEFORE_CLOSE = int(config.get("buy_window_minutes_before_close", 30))
PORTFOLIO_HEAT_MAX_PCT = float(config.get("portfolio_heat_max_pct", 0.06))

# Phase 4: VIX / Fear&Greed 거시 방어막 (매 루프 `get_macro_guard_snapshot(config)` 로 적용)
# config: macro_guard_enabled, macro_us_put_call_*, macro_coin_whale_*, macro_krw_fx_momentum_*

# 📊 [지수 급락 기준] 각 시장의 신규 매수 중단 임계값
INDEX_CRASH_KR = -3.0     # 국장 KOSPI 급락 기준 (%)
INDEX_CRASH_US = -1.8     # 미장 S&P500 급락 기준 (%)
INDEX_CRASH_COIN = -3.5   # 코인 BTC 급락 기준 (%)

WEATHER_LABEL_BEAR = "🌧️ BEAR"


def _v8_trend_buy_allowed_in_weather(weather_label: str) -> bool:
    """BEAR 시장에서는 V8(추세) 신규 매수만 차단. ``SWING_FIB`` 스윙은 허용."""
    return str(weather_label or "").strip() != WEATHER_LABEL_BEAR

# 업비트 코인 시장가 매수 — 가용 잔고 캡(수수료·반올림 오차로 InsufficientFundsBid 방지)
UPBIT_KRW_AVAILABLE_CAP_RATIO = 0.999  # 주문 직전: min(목표액, get_balance(KRW) * 이 값)
UPBIT_COIN_MIN_ORDER_KRW = 5000.0      # KRW 마켓 최소 주문 금액(업비트 기준)
# 바이낸스: 24h USDT 거래대금 상위 N (`binance_universe_top`, 기본 10)
BINANCE_UNIVERSE_TOP = int(config.get("binance_universe_top", 10))
# 업비트: KRW 마켓 거래대금 상위 N (`upbit_universe_top`, 기본 10)
UPBIT_UNIVERSE_TOP = int(config.get("upbit_universe_top", 10))
# 코인(업비트·바이낸스 공통): ``_is_coin_buy_window_now`` 일봉 직전 창만 매수. V8→스윙 순서는 국·미장과 동일.


def _coin_min_order_krw() -> float:
    """코인 최소 주문(원화 환산). 바이낸스는 USDT 최소명목×환율."""
    try:
        return float(coin_broker.min_order_budget_krw())
    except Exception:
        return float(UPBIT_COIN_MIN_ORDER_KRW)


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
# 국장 KIS 일봉 연속 호출 간격(초). 전역 limiter와 병행 — 기본 0.35(실전 20건/초 여유)
KIS_OHLCV_MIN_INTERVAL_SEC = float(config.get("kis_ohlcv_min_interval_sec", 0.35))


def _throttle_kis_ohlcv():
    """KIS domestic OHLCV TR 연속 호출 시 전역 한도와 로컬 간격을 함께 적용."""
    global _kis_ohlcv_last_ts
    try:
        from api.kis_rate_limit import wait_for_slot

        wait_for_slot(label="ohlcv")
    except Exception:
        pass
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
            ohlcv = get_cached_ohlcv(t, broker if use_kis_first else None)

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


def _store_ohlcv_cache(ticker: str, ohlcv: list) -> list:
    """메모리·디스크 캐시에 저장 후 동일 리스트 반환."""
    try:
        from utils.ohlcv_store import normalize_ohlcv_series, ohlcv_series_valid, save_disk_ohlcv

        series = normalize_ohlcv_series(ohlcv)
        if series and len(series) >= 14 and ohlcv_series_valid(series):
            _ohlcv_cache[ticker] = series
            save_disk_ohlcv(ticker, series)
            return series
    except Exception:
        pass
    if ohlcv and len(ohlcv) >= 14:
        _ohlcv_cache[ticker] = ohlcv
    return ohlcv


def _ohlcv_candidate_rank(rows: list) -> tuple[int, int]:
    """(유효 여부 우선, 봉 수) — ``get_cached_ohlcv`` 소스 선택."""
    try:
        from utils.ohlcv_store import ohlcv_series_valid

        ok = 1 if ohlcv_series_valid(rows) else 0
    except Exception:
        ok = 1 if rows and len(rows) >= 14 else 0
    return (ok, len(rows or []))


def get_cached_ohlcv(ticker, broker=None, force_refresh: bool = False):
    """OHLCV 확보(200봉). 메모리·디스크 → 국장 KIS→pykrx → 미장 KIS→Stooq(키)→yfinance."""
    try:
        from utils.ohlcv_store import (
            invalidate_disk_ohlcv,
            load_disk_ohlcv,
            normalize_ohlcv_series,
            ohlcv_series_valid,
        )
    except Exception:
        load_disk_ohlcv = lambda _t, **kw: None  # type: ignore
        invalidate_disk_ohlcv = lambda _t: None  # type: ignore
        normalize_ohlcv_series = lambda r: r or []  # type: ignore
        ohlcv_series_valid = lambda r, **kw: bool(r)  # type: ignore

    t = str(ticker or "").strip()
    if force_refresh:
        _ohlcv_cache.pop(t, None)
        try:
            invalidate_disk_ohlcv(t)
        except Exception:
            pass

    mem = _ohlcv_cache.get(t) or []
    if (
        not force_refresh
        and mem
        and len(mem) >= 200
        and ohlcv_series_valid(mem)
    ):
        return mem

    disk = None if force_refresh else load_disk_ohlcv(t)
    if disk and len(disk) >= 200 and ohlcv_series_valid(disk):
        _ohlcv_cache[t] = disk
        return disk
    if disk and not ohlcv_series_valid(disk):
        try:
            invalidate_disk_ohlcv(t)
        except Exception:
            pass

    kr_digit = broker is not None and str(ticker).isdigit()
    best: list = []

    def _take(candidate: list) -> None:
        nonlocal best
        if not candidate:
            return
        series = normalize_ohlcv_series(candidate)
        if not series:
            return
        if _ohlcv_candidate_rank(series) > _ohlcv_candidate_rank(best):
            best = series

    # 국장: KIS + pykrx(기준) 교차검증 — 미장과 동일(날짜·정렬·꼬리 검증)
    if kr_digit:
        _throttle_kis_ohlcv()
        kis_kr: list = []
        try:
            kis_kr = get_ohlcv_kis_domestic_daily(broker, ticker) or []
        except Exception as e:
            print(f"     ⚠️ [{ticker}] KIS 일봉 조회 예외: {e}")
        ref_kr = get_ohlcv_pykrx(ticker) or []
        ref_label = "pykrx"
        if not ref_kr:
            try:
                ref_kr = get_ohlcv_yfinance(ticker) or []
                ref_label = "yfinance"
            except Exception as e:
                print(f"     ⚠️ [{ticker}] yfinance(국장검증) 조회 실패: {e}")
        try:
            from utils.ohlcv_store import select_validated_kr_ohlcv

            picked = select_validated_kr_ohlcv(
                kis_kr, ref_kr, ticker=ticker, reference_name=ref_label
            )
            if picked:
                best = picked
        except Exception:
            if ref_kr:
                best = normalize_ohlcv_series(ref_kr)
            elif kis_kr:
                best = normalize_ohlcv_series(kis_kr)
        if len(best) >= 200:
            return _store_ohlcv_cache(ticker, best)
        if len(best) < 200 and ref_kr:
            _take(ref_kr)
        if len(best) >= 200:
            return _store_ohlcv_cache(ticker, best)
        if kis_kr and len(kis_kr) < 200:
            print(f"     ⚠️ [{ticker}] KIS {len(kis_kr)}봉 — pykrx/yfinance 보강")

    # 미장: KIS + yfinance 교차검증 → Stooq 보강 (맹목 KIS 단일 사용 금지)
    yf_ref: list = []
    if not kr_digit:
        try:
            yf_ref = get_ohlcv_yfinance(ticker) or []
        except Exception as e:
            print(f"     ⚠️ [{ticker}] yfinance 조회 실패: {e}")
        kis_us: list = []
        if not kis_equities_weekend_suppress_window_kst():
            try:
                from api import kis_api
                from api.kis_api import get_ohlcv_kis_us_daily

                bus = kis_api.broker_us
                if bus:
                    kis_us = get_ohlcv_kis_us_daily(bus, ticker) or []
            except Exception as e:
                print(f"     ⚠️ [{ticker}] KIS 해외 일봉 예외: {e}")
        try:
            from utils.ohlcv_store import select_validated_equity_ohlcv

            picked = select_validated_equity_ohlcv(kis_us, yf_ref, ticker=ticker)
            if picked:
                best = picked
        except Exception:
            if yf_ref:
                best = normalize_ohlcv_series(yf_ref)
            elif kis_us:
                best = normalize_ohlcv_series(kis_us)

    stooq_key = str(config.get("stooq_apikey", "") or "").strip()
    if stooq_key and len(best) < 200:
        _take(get_ohlcv_stooq(ticker, apikey=stooq_key) or [])
    if len(best) >= 200:
        return _store_ohlcv_cache(ticker, best)

    if not kr_digit and yf_ref and len(best) < 200:
        _take(yf_ref)
        if yf_ref and len(yf_ref) < 200:
            print(f"     ⚠️ [{ticker}] yfinance {len(yf_ref)}봉 (<200)")
    elif kr_digit:
        try:
            ohlcv_yf = get_ohlcv_yfinance(ticker) or []
            _take(ohlcv_yf)
            if ohlcv_yf and len(ohlcv_yf) < 200:
                print(f"     ⚠️ [{ticker}] yfinance {len(ohlcv_yf)}봉 (<200)")
        except Exception as e:
            print(f"     ⚠️ [{ticker}] yfinance 조회 실패: {e}")

    if len(best) >= 200:
        return _store_ohlcv_cache(ticker, best)
    if best:
        _store_ohlcv_cache(ticker, best)
        return best

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

def _record_trade_event(
    market,
    ticker,
    side,
    qty,
    price=None,
    profit_rate=None,
    reason="",
    name="",
    ledger=None,
):
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

    sector = ""
    try:
        sector = resolve_trade_sector(market, ticker)
    except Exception:
        sector = ""

    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market": market,
        "ticker": ticker,
        "name": symbol_name,
        "sector": sector,
        "side": side,
        "qty": qty,
        "price": price,
        "profit_rate": profit_rate,
        "reason": reason,
    }
    if str(side or "").upper() == "BUY" and isinstance(ledger, dict):
        from services.trade_history_ledger import ledger_extra_from_buy_payload

        record.update(ledger_extra_from_buy_payload(ledger))
    record_trade(record)


def _coin_sell_order_ok(resp) -> bool:
    """코인 시장가 매도 응답 — 빈 dict 등 falsy 오판 방지."""
    if resp is None or resp is False:
        return False
    return True


def _telegram_swing_sell(
    market: str,
    ticker: str,
    *,
    name: str = "",
    half: bool,
    qty_label: str,
    profit_rate: float,
    reason: str,
) -> None:
    """스윙 HALF/FULL 체결 텔레그램 (KR/US/COIN 공통)."""
    mk = str(market or "").strip().upper()
    label = str(name or "").strip() or str(ticker)
    title = "스윙 50% 익절" if half else "스윙 전량 청산"
    emoji = "💰" if half else "🚨"
    reason_short = str(reason or "").strip()
    if len(reason_short) > 280:
        reason_short = reason_short[:277] + "..."
    send_telegram(
        f"{emoji} [{mk} {title}] {ticker}({label})\n"
        f"수량: {qty_label}\n"
        f"수익률: {float(profit_rate):+.2f}%\n"
        f"사유: {reason_short}"
    )


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
    *,
    force: bool = False,
) -> None:
    """
    KIS 조회가 성공한 직후 호출 — 주말 창에서는 덮어쓰지 않음(직전 평일 값 유지).

    ``force=True`` (GUI ``KIS 강제 새로고침`` 등) 이면 점검 창에서도 스냅샷을 갱신한다.
    """
    if kis_equities_weekend_suppress_window_kst() and not force:
        return
    from services import ledger_valuation as _lv

    st = load_state(STATE_PATH)
    saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _lv.write_kis_display_snapshot_part(
        st,
        "KR",
        cash=float(d2_kr),
        total=float(kr_total),
        roi=kr_hold_roi,
        saved_at=saved_at,
        force=bool(force),
    )
    _lv.write_kis_display_snapshot_part(
        st,
        "US",
        cash=float(us_cash),
        total=float(us_total),
        roi=us_hold_roi,
        saved_at=saved_at,
        force=bool(force),
    )
    save_state(STATE_PATH, st)


def load_last_coin_display_snapshot() -> dict:
    """``bot_state.json`` 의 ``last_coin_display_snapshot`` — 코인 예수·총평·ROI(원화환산 정수).

    잔고 API 실패 시 GUI·텔레 라벨 폴백용. 보유 종목 표는 여전히 실조회·장부에 의존.
    """
    st = load_state(STATE_PATH)
    x = st.get("last_coin_display_snapshot")
    return x if isinstance(x, dict) else {}


def save_last_coin_display_snapshot(cash_krw: int, total_krw: int, roi) -> None:
    """코인 라벨 조회 성공 직후 저장. 국·미와 달리 **주말에도 갱신**(코인 시장 상시).

    ``cash_krw`` / ``total_krw`` 는 **업비트=원**, **바이낸스=USDT를 원화 환산한 정수**
    (스냅샷 ``labels["coin"]`` 과 동일 스키마). 서킷·Phase5와 별개인 **표시용** 직전 성공 값.
    """
    st = load_state(STATE_PATH)
    st["last_coin_display_snapshot"] = {
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cash": int(cash_krw),
        "total": int(total_krw),
        "roi": roi,
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
    return account_read_facade.get_held_stocks_kr(
        is_weekend=kis_equities_weekend_suppress_window_kst,
        is_market_open=is_market_open,
        load_state=load_state,
        state_path=STATE_PATH,
        get_balance_with_retry=get_balance_with_retry,
        to_float=_to_float,
        normalize_ticker=normalize_ticker,
    )

def get_held_stocks_us():
    """🇺🇸 미장 실제 보유 종목 티커 리스트 가져오기
    성공: list 반환 (빈 리스트도 정상)
    실패: None 반환
    """
    return account_read_facade.get_held_stocks_us(
        is_weekend=kis_equities_weekend_suppress_window_kst,
        is_market_open=is_market_open,
        load_state=load_state,
        state_path=STATE_PATH,
        get_us_positions_with_retry=get_us_positions_with_retry,
        to_float=_to_float,
        normalize_ticker=normalize_ticker,
    )

def get_held_coins():
    """🪙 코인 실제 보유 티커 리스트 가져오기
    성공: list 반환 (빈 리스트도 정상)
    실패: None 반환
    """
    try:
        balances = coin_broker.get_balances()
        if not balances:
            print(f"❌ [코인 조회 실패] 잔고 API 응답 없음")
            return None
        held = []
        for b in balances:
            t = coin_broker.held_ticker_row(b)
            if t and coin_broker.should_include_coin_balance_row(b):
                held.append(t)
        return held
    except Exception as e:
        print(f"❌ [코인 조회 실패] {type(e).__name__}: {e}")
        return None


def kis_equities_ready_for_trading_cycle() -> bool:
    """
    개장 중인 KIS 시장(국·미)은 실보유 조회가 성공해야 매매 사이클을 돌린다.

    주말·점검 창은 장부·스냅샷 경로만 쓰므로 True.
    ``kis_balance_sync_mode=on_trade``(봇 전용)이면 KIS 잔고 없이도 True.
    """
    if kis_equities_weekend_suppress_window_kst():
        return True
    from execution.balance_policy import is_on_trade_balance_mode

    if is_on_trade_balance_mode(config):
        return True
    if is_market_open("KR"):
        held_kr = get_held_stocks_kr()
        if held_kr is None:
            print("  ⏸️ [시스템] 국장 KIS 보유 조회 실패 — 이번 매매 사이클 보류")
            return False
    if is_market_open("US"):
        held_us = get_held_stocks_us()
        if held_us is None:
            print("  ⏸️ [시스템] 미장 KIS 보유 조회 실패 — 이번 매매 사이클 보류")
            return False
    return True


def fetch_equity_held_lists_for_position_sync():
    """국·미 **정규장**일 때만 KIS로 보유 티커를 조회한다.

    비장중에는 API를 호출하지 않고 ``[]`` 를 반환한다(보유 목록 갱신 생략).
    ``sync_all_positions`` 는 비장중 KIS 시드를 쓰지 않으며 장부로 유령 판정을 보강한다.
    """
    skipped = []
    if is_market_open("KR"):
        held_kr = get_held_stocks_kr()
    else:
        skipped.append("국장")
        held_kr = []
    if is_market_open("US"):
        held_us = get_held_stocks_us()
    else:
        skipped.append("미장")
        held_us = []
    if skipped:
        print(
            f"  💤 [보유 조회] {'·'.join(skipped)} 비장중 — KIS 보유 목록 갱신 생략 (코인·장부 동기화는 계속)"
        )
    return held_kr, held_us


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

    suppress_kr_us_yahoo = kis_equities_weekend_suppress_window_kst()
    # -----------------------------------------------------------
    # 🇰🇷 국장 날씨 (KODEX 200 - yfinance). 주말·KIS 점검 창에서는 Yahoo 생략(횡보장 유지).
    # -----------------------------------------------------------
    if suppress_kr_us_yahoo:
        print("  📌 [기상] KIS 주말·점검 창 — 국·미 날씨 Yahoo 생략 (코인만 실조회)")
    if not suppress_kr_us_yahoo:
        try:
            from utils.yfinance_guard import yf_call

            df_kr = yf_call(
                lambda: yf.Ticker("069500.KS").history(period="2mo"),
                label="weather_kr",
                ticker="069500.KS",
            )
            if df_kr is None:
                df_kr = pd.DataFrame()
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
    # 🇺🇸 미장 날씨 (SPY - S&P 500 ETF). 주말·점검 창에서는 위와 같이 생략됨.
    # -----------------------------------------------------------
    if not suppress_kr_us_yahoo:
        try:
            from utils.yfinance_guard import yf_call

            df_us = yf_call(
                lambda: yf.Ticker("SPY").history(period="2mo"),
                label="weather_us",
                ticker="SPY",
            )
            if df_us is None:
                df_us = pd.DataFrame()
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
        bt = coin_config.btc_benchmark_ticker()
        rows = coin_broker.fetch_ohlcv(bt, "day", 40)
        if rows and len(rows) >= 30:
            df_coin = pd.DataFrame(rows)
            df_coin = df_coin.rename(
                columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
            )
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
    return account_read_facade.get_held_stocks_kr_info(
        is_weekend=kis_equities_weekend_suppress_window_kst,
        is_market_open=is_market_open,
        load_state=load_state,
        state_path=STATE_PATH,
        get_balance_with_retry=get_balance_with_retry,
        to_float=_to_float,
        kr_name_dict=kr_name_dict,
        ledger_qty_for_ui=_ledger_qty_for_ui,
    )

def get_held_stocks_us_info():
    """미국 보유 주식 정보"""
    return account_read_facade.get_held_stocks_us_info(
        is_weekend=kis_equities_weekend_suppress_window_kst,
        is_market_open=is_market_open,
        load_state=load_state,
        state_path=STATE_PATH,
        get_us_positions_with_retry=get_us_positions_with_retry,
        to_float=_to_float,
        us_name_dict=us_name_dict,
        ledger_qty_for_ui=_ledger_qty_for_ui,
    )

def get_held_stocks_us_detail():
    """미국 보유 주식 상세 (GUI용으로 변환)"""
    return account_read_facade.get_held_stocks_us_detail(
        is_weekend=kis_equities_weekend_suppress_window_kst,
        is_market_open=is_market_open,
        load_state=load_state,
        state_path=STATE_PATH,
        get_us_positions_with_retry=get_us_positions_with_retry,
        to_float=_to_float,
        ledger_qty_for_ui=_ledger_qty_for_ui,
    )

def get_held_stocks_coins_info():
    """코인 보유 정보"""
    try:
        balances = coin_broker.get_balances()
        coins = []
        for b in balances:
            if b.get("currency") in ("KRW", "VTHO"):
                continue
            if coin_config.is_binance() and str(b.get("currency", "")).upper() == "USDT":
                continue
            qty = _to_float(b.get('balance'))
            if coin_broker.should_include_coin_balance_row(b):
                ticker = coin_broker.held_ticker_row(b)
                if not ticker:
                    continue
                price = coin_broker.get_current_price(ticker) or 0
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
            try:
                return ensure_dict(bal_read.kr_balance_for_report(refresh=False))
            except Exception:
                return {}
        elif market == "US":
            if kis_equities_weekend_suppress_window_kst():
                return {}
            try:
                return ensure_dict(bal_read.us_balance_for_report(refresh=False))
            except Exception:
                return {}
        elif market == "COIN":
            try:
                raw = bal_read.coin_balances_for_report(refresh=False)
                return raw if isinstance(raw, list) else []
            except Exception:
                return []
        return {}
    
    # 현재: dict 값 추출 모드
    if isinstance(data, dict):
        return data.get(key, default)
    return default


def _manual_sell_remaining_display(market: str, remaining_qty: float) -> str:
    """로그·텔레그램용 잔여 수량 문자열."""
    rq = float(remaining_qty or 0.0)
    if market == "COIN":
        s = f"{rq:.8f}".rstrip("0").rstrip(".")
        return s if s else "0"
    return str(int(round(rq)))


def _run_manual_sell_position_sync() -> None:
    """수동 매도 직후 실계좌 시드와 장부 재동기화(국·미·코인 조회가 모두 성공할 때만)."""
    state = load_state(STATE_PATH)
    held_kr = get_held_stocks_kr()
    held_us = get_held_stocks_us()
    held_coins = get_held_coins()
    if held_kr is None or held_us is None or held_coins is None:
        print(
            "  ⚠️ [수동 매도 후 동기화 건너뜀] 실보유 조회 실패 — 다음 자동 사이클에서 재동기화됩니다."
        )
        return
    sync_all_positions(state, held_kr, held_us, held_coins, STATE_PATH)


def _apply_manual_sell_state_update(
    ticker: str,
    exec_price: float,
    market: str,
    sold_qty: float,
    *,
    state: dict | None = None,
    idem_lane: str | None = None,
) -> dict:
    """수동 매도 체결 후 장부 반영. 전량 청산 시에만 승패·total_profit·티커 쿨다운(Layer2) 종결."""
    if state is None:
        state = load_state(STATE_PATH)
    else:
        ledger_apply.merge_disk_if_newer(state, STATE_PATH)
    ticker = normalize_ticker(ticker)
    positions = state.setdefault("positions", {})
    pos_info = positions.get(ticker, {}) or {}
    strategy_st = pos_info.get("strategy_type")
    buy_p = _to_float(pos_info.get("buy_p", 0), 0.0)
    exec_px = _to_float(exec_price, 0.0)

    qty_before = _to_float(pos_info.get("qty", 0), 0.0)
    sold_eff = float(sold_qty)
    if qty_before > 0:
        sold_eff = min(sold_eff, qty_before)

    eps = 1e-8 if market == "COIN" else 1e-6
    if qty_before <= 0:
        remaining = 0.0
        full_exit = True
    else:
        remaining = max(0.0, qty_before - sold_eff)
        full_exit = remaining <= eps

    profit_rate = None
    if buy_p > 0 and exec_px > 0:
        profit_rate = ((exec_px - buy_p) / buy_p) * 100.0

    stats = state.setdefault("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})

    if full_exit:
        if profit_rate is not None:
            if profit_rate > 0:
                stats["wins"] = int(stats.get("wins", 0) or 0) + 1
            else:
                stats["losses"] = int(stats.get("losses", 0) or 0) + 1
            stats["total_profit"] = float(stats.get("total_profit", 0.0) or 0.0) + float(profit_rate)
    else:
        if profit_rate is not None and qty_before > 0:
            stats.setdefault("manual_partial_total_profit_pct", 0.0)
            w = float(sold_eff) / float(qty_before)
            stats["manual_partial_total_profit_pct"] = float(stats["manual_partial_total_profit_pct"]) + float(
                profit_rate
            ) * w

    def _mut_manual_sell(st: dict) -> None:
        set_cooldown(st, ticker)
        cd_reason = (
            "Phase5 서킷 청산"
            if str(idem_lane or "").strip() == order_idem.LANE_PHASE5
            else "수동 매도"
        )
        set_ticker_cooldown_after_sell(
            st,
            ticker,
            cd_reason,
            profit_rate=profit_rate,
            strategy_type=strategy_st,
            market=market,
            remaining_qty=float(remaining) if not full_exit else 0.0,
        )

    ctx = f"수동매도 {market} {ticker}"
    if full_exit:
        if ticker in positions:
            ledger_apply.persist_position_remove(
                state, ticker, context=ctx, state_path=STATE_PATH, mutate_fn=_mut_manual_sell
            )
        else:
            _mut_manual_sell(state)
            ledger_apply.save_state_verified(state, STATE_PATH, context=ctx)
    elif qty_before > 0 and remaining > eps:
        new_pos = post_partial_ledger(
            dict(pos_info),
            float(sold_eff),
            float(exec_px),
            float(qty_before),
            set_scale_out_done=False,
        )
        ledger_apply.persist_position_set(
            state, ticker, new_pos, context=ctx, state_path=STATE_PATH, mutate_fn=_mut_manual_sell
        )
    else:
        _mut_manual_sell(state)
        ledger_apply.save_state_verified(state, STATE_PATH, context=ctx)

    return {
        "profit_rate": profit_rate,
        "full_exit": full_exit,
        "remaining_qty": float(remaining) if not full_exit else 0.0,
        "sold_qty": float(sold_eff),
    }

def manual_sell(market, code, quantity, *, idem_lane: str | None = None):
    """수동 매도
    반환 형식: {"success": bool, "message": str}

    ``idem_lane`` — Phase5 청산 등 멱등 lane (기본 ``manual``).
    """
    lane = str(idem_lane or order_idem.LANE_MANUAL)
    try:
        qty = _to_float(quantity, 0)
        if qty <= 0:
            return {"success": False, "message": "매도 수량이 0 이하입니다."}

        st = load_state(STATE_PATH)
        order_idem.ensure_idempotency_state(st)

        if market == "KR":
            # 현재가 먼저 조회
            ohlcv = get_ohlcv_realtime(kis_api.broker_kr, code)
            curr_p = _to_float(ohlcv[-1].get('c', 0), 0.0) if ohlcv else 0.0
            if curr_p <= 0:
                return {"success": False, "message": "국장 현재가 조회 실패"}
            
            def _man_kr_place():
                return create_market_sell_order_kis(
                    code, int(qty), is_us=False, curr_price=curr_p
                )

            fill = _idempotent_kis_sell(
                st,
                market="KR",
                ticker=code,
                lane=lane,
                qty=int(qty),
                fallback_price=float(curr_p),
                place_order=_man_kr_place,
            )
            ok = fill.ok
            msg = fill.note or ("체결" if ok else "국장 매도 실패")
            if ok:
                order_idem.persist_idempotency(st, STATE_PATH)
                bal_read.invalidate("KR")
                pos0 = (st.get("positions") or {}).get(code, {})
                hold_note = _holding_duration_suffix(
                    pos0 if isinstance(pos0, dict) else {}, "KR"
                )
                exec_price = curr_p
                meta = _apply_manual_sell_state_update(
                    code, exec_price, "KR", float(int(qty)), state=st, idem_lane=lane
                )
                profit_rate = meta.get("profit_rate")
                full_exit = bool(meta.get("full_exit", True))
                rem_q = float(meta.get("remaining_qty") or 0.0)
                _record_trade_event("KR", code, "SELL", int(qty), price=exec_price if exec_price > 0 else None, profit_rate=profit_rate, reason="MANUAL")
                kr_name = get_kr_company_name(code)
                profit_str = f"{profit_rate:+.2f}%" if profit_rate is not None else "N/A"
                if full_exit:
                    print(
                        f"  ✅ [국장 수동매도 체결] {kr_name}({code}) {int(qty)}주 (전량) | 수익률: {profit_str}"
                    )
                    send_telegram(f"✅ [KR] {code}({kr_name}) {int(qty)}주 수동 매도 완료 (전량 청산){hold_note}")
                else:
                    rem_disp = _manual_sell_remaining_display("KR", rem_q)
                    print(
                        f"  ✅ [국장 수동매도 체결] {kr_name}({code}) 부분 {int(qty)}주 | 잔여 약 {rem_disp}주 | 구간수익률: {profit_str}"
                    )
                    send_telegram(
                        f"✅ [KR] {code}({kr_name}) 부분 매도 {int(qty)}주 완료 · 장부 잔여 약 {rem_disp}주{hold_note}"
                    )
                _run_manual_sell_position_sync()
                _refresh_kis_display_snapshot_after_trade(st, "KR")
                return {"success": True, "message": msg}
            return {"success": False, "message": msg}

        if market == "US":
            # 수동매도는 시장가로 처리
            us_bal = (
                {}
                if kis_equities_weekend_suppress_window_kst()
                else ensure_dict(bal_read.us_balance_for_report(refresh=False))
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

            def _man_us_place():
                return execute_us_order_direct(
                    kis_api.broker_us, "sell", code, int(qty), current_price
                )

            fill = _idempotent_kis_sell(
                st,
                market="US",
                ticker=code,
                lane=lane,
                qty=int(qty),
                fallback_price=float(current_price),
                place_order=_man_us_place,
            )
            ok = fill.ok
            msg = fill.note or ("체결" if ok else "미장 매도 실패")
            if ok:
                order_idem.persist_idempotency(st, STATE_PATH)
                bal_read.invalidate("US")
                pos0 = (st.get("positions") or {}).get(code, {})
                hold_note = _holding_duration_suffix(
                    pos0 if isinstance(pos0, dict) else {}, "US"
                )
                meta = _apply_manual_sell_state_update(
                    code, current_price, "US", float(int(qty)), state=st, idem_lane=lane
                )
                profit_rate = meta.get("profit_rate")
                full_exit = bool(meta.get("full_exit", True))
                rem_q = float(meta.get("remaining_qty") or 0.0)
                _record_trade_event("US", code, "SELL", int(qty), price=current_price, profit_rate=profit_rate, reason="MANUAL")
                us_name = get_us_company_name(code)
                profit_str = f"{profit_rate:+.2f}%" if profit_rate is not None else "N/A"
                if full_exit:
                    print(
                        f"  ✅ [미장 수동매도 체결] {us_name}({code}) {int(qty)}주 (전량) | 수익률: {profit_str}"
                    )
                    send_telegram(f"✅ [US] {code}({us_name}) {int(qty)}주 수동 매도 완료 (전량 청산){hold_note}")
                else:
                    rem_disp = _manual_sell_remaining_display("US", rem_q)
                    print(
                        f"  ✅ [미장 수동매도 체결] {us_name}({code}) 부분 {int(qty)}주 | 잔여 약 {rem_disp}주 | 구간수익률: {profit_str}"
                    )
                    send_telegram(
                        f"✅ [US] {code}({us_name}) 부분 매도 {int(qty)}주 완료 · 장부 잔여 약 {rem_disp}주{hold_note}"
                    )
                _run_manual_sell_position_sync()
                _refresh_kis_display_snapshot_after_trade(st, "US")
                return {"success": True, "message": msg}
            current_p = _to_float(coin_broker.get_current_price(code) or 0, 0.0)
            fill = _idempotent_coin_sell(
                st,
                ticker=code,
                lane=lane,
                qty=float(qty),
                fallback_price=float(current_p),
            )
            if fill.ok:
                order_idem.persist_idempotency(st, STATE_PATH)
                bal_read.invalidate("COIN")
                pos0 = (st.get("positions") or {}).get(code, {})
                hold_note = _holding_duration_suffix(
                    pos0 if isinstance(pos0, dict) else {}, "COIN"
                )
                meta = _apply_manual_sell_state_update(
                    code, current_p, "COIN", float(qty), state=st, idem_lane=lane
                )
                profit_rate = meta.get("profit_rate")
                full_exit = bool(meta.get("full_exit", True))
                rem_q = float(meta.get("remaining_qty") or 0.0)
                _record_trade_event("COIN", code, "SELL", qty, price=current_p if current_p > 0 else None, profit_rate=profit_rate, reason="MANUAL")
                profit_str = f"{profit_rate:+.2f}%" if profit_rate is not None else "N/A"
                if full_exit:
                    print(f"  ✅ [코인 수동매도 체결] {code} {qty} (전량) | 수익률: {profit_str}")
                    send_telegram(f"✅ [COIN] {code} {qty} 수동 매도 완료 (전량 청산){hold_note}")
                else:
                    rem_disp = _manual_sell_remaining_display("COIN", rem_q)
                    print(
                        f"  ✅ [코인 수동매도 체결] {code} 부분 {qty} | 잔여 약 {rem_disp} | 구간수익률: {profit_str}"
                    )
                    send_telegram(
                        f"✅ [COIN] {code} 부분 매도 {qty} 완료 · 장부 잔여 약 {rem_disp}{hold_note}"
                    )
                _run_manual_sell_position_sync()
                _refresh_kis_display_snapshot_after_trade(st, "COIN")
                return {"success": True, "message": "코인 시장가 매도 요청 완료"}
            return {"success": False, "message": fill.note or "코인 매도 실패"}

        return {"success": False, "message": f"지원하지 않는 시장 코드: {market}"}
    except Exception as e:
        err = str(e)
        send_telegram(f"🚨 [{market}] {code} 수동 매도 실패: {err}")
        return {"success": False, "message": err}


def _portfolio_total_krw_from_aux(state: dict) -> float:
    """직전 루프 시장별 합산 + 현재 환율로 원화 합산 (국·미는 스냅샷)."""
    from services.ledger_valuation import kis_display_total

    rate = estimate_usdkrw()
    kr = kis_display_total(state, "KR")
    coin = float(state.get("circuit_aux_last_coin_krw", 0) or 0)
    usd = kis_display_total(state, "US")
    return kr + coin + usd * rate


def refresh_circuit_aux_from_brokers(state: dict, path: Path) -> dict:
    """
    Phase5 보조키 ``circuit_aux_last_*`` 를 브로커·업비트 최신 값으로 갱신.

    * 평일: KIS 국·미 잔고 + 업비트 코인.
    * 주말 점검 창(KIS 미호출): 국·미는 ``last_kis_display_snapshot`` 직전 값,
      코인만 실조회.

    ``adjust_capital.py`` 등에서 입출금 직후 합산 총액을 맞추기 위해 호출한다.
    """
    result = {
        "kr_ok": False,
        "us_ok": False,
        "coin_ok": False,
        "weekend_kis_skip": False,
        "totals": {},
    }
    try:
        refresh_brokers_if_needed(force=False)
    except Exception:
        pass

    path = Path(path)
    from services import ledger_valuation as lv

    prev_kr_equity = lv.kis_display_total(state, "KR")
    prev_us_equity = lv.kis_display_total(state, "US")
    prev_coin_equity = float(state.get("circuit_aux_last_coin_krw", 0) or 0)
    total_kr_equity = prev_kr_equity
    total_us_equity = prev_us_equity
    total_coin_equity = prev_coin_equity
    kr_cash_live = 0.0
    us_cash_live = 0.0

    suppress = kis_equities_weekend_suppress_window_kst()
    if suppress:
        result["weekend_kis_skip"] = True
        snap = load_last_kis_display_snapshot()
        kr_d = snap.get("kr") or {}
        us_d = snap.get("us") or {}
        if isinstance(kr_d, dict) and kr_d.get("total") is not None:
            total_kr_equity = float(kr_d["total"])
            result["kr_ok"] = True
        if isinstance(us_d, dict) and us_d.get("total") is not None:
            total_us_equity = float(us_d["total"])
            result["us_ok"] = True
    else:
        try:
            if is_market_open("KR"):
                from api.kis_parsers import kis_response_ok

                bal = ensure_dict(bal_read.kr_balance_raw(refresh=False))
                ok, why = kis_response_ok(bal, require_output2=True)
                if ok:
                    kr_balance_data = bal.get("output2", [])
                    kr_cash_live, parsed_kr = parse_kr_cash_total(kr_balance_data, _to_float)
                    suspicious_kr = (
                        prev_kr_equity > 500_000.0
                        and float(parsed_kr) > 0.0
                        and float(parsed_kr) < prev_kr_equity * 0.35
                    )
                    suspicious_zero = prev_kr_equity > 500_000.0 and float(parsed_kr) <= 0.0
                    if suspicious_kr or suspicious_zero:
                        print(
                            "  ⚠️ [circuit_aux 갱신] 국장 총평가 급락/0원(일시 API 이상 추정) — "
                            f"직전 {prev_kr_equity:,.0f}원 유지, kr_ok=False ({why})"
                        )
                    else:
                        total_kr_equity = float(parsed_kr)
                        result["kr_ok"] = True
                else:
                    print(
                        f"  ⚠️ [circuit_aux 갱신] 국장 잔고 응답 비정상 — 직전 값 유지, kr_ok=False ({why})"
                    )
            else:
                snap = load_last_kis_display_snapshot()
                kr_d = snap.get("kr") or {}
                if isinstance(kr_d, dict) and kr_d.get("total") is not None:
                    total_kr_equity = float(kr_d["total"])
                    result["kr_ok"] = True
                print("  📌 [circuit_aux 갱신] 국장 비장중 — last_kis_display_snapshot 사용")
        except Exception as e:
            print(f"  ⚠️ [circuit_aux 갱신] 국장 조회 실패: {e}")

        try:
            if is_market_open("US"):
                from api.kis_parsers import kis_response_ok

                us_cash = float(get_us_cash_real(kis_api.broker_us) or 0.0)
                us_bal = ensure_dict(bal_read.us_balance_raw(refresh=False))
                ok_us, why_us = kis_response_ok(us_bal, require_output2=True)
                if not ok_us:
                    print(
                        f"  ⚠️ [circuit_aux 갱신] 미장 잔고 응답 비정상 — 직전 값 유지, us_ok=False ({why_us})"
                    )
                else:
                    out2 = safe_get(us_bal, "output2", {})
                    if us_cash <= 0.0 and out2:
                        try:
                            us_cash = float(parse_us_cash_fallback(out2, _to_float))
                        except Exception:
                            pass
                    us_output1 = ensure_list(us_bal.get("output1", []))
                    if isinstance(out2, list) and out2:
                        us_stock_value = _to_float(out2[0].get("ovrs_stck_evlu_amt", 0))
                    elif isinstance(out2, dict):
                        us_stock_value = _to_float(out2.get("ovrs_stck_evlu_amt", 0))
                    else:
                        us_stock_value = 0.0
                    if us_stock_value <= 0 and us_output1:
                        manual_stock_eval = 0.0
                        for s in us_output1:
                            val = _to_float(s.get("frcr_evlu_amt2", 0))
                            if val <= 0:
                                price = _to_float(s.get("ovrs_now_prc2", 0))
                                qty = _to_float(s.get("ovrs_cblc_qty", s.get("hldg_qty", 0)))
                                val = price * qty
                            manual_stock_eval += val
                        if manual_stock_eval > 0:
                            us_stock_value = manual_stock_eval
                    parsed_us = us_cash + us_stock_value
                    suspicious_zero = (parsed_us <= 0.0) and (
                        prev_us_equity > 0.0 or bool(us_output1)
                    )
                    suspicious_drop = (
                        prev_us_equity > 0.0
                        and parsed_us > 0.0
                        and parsed_us < prev_us_equity * 0.35
                    )
                    if suspicious_zero or suspicious_drop:
                        result["us_ok"] = False
                        print(
                            "  ⚠️ [circuit_aux 갱신] 미장 값 비정상(미수금/총평가 누락 추정) — "
                            "직전 US 유지, Phase5 판정에서 제외"
                        )
                    else:
                        total_us_equity = float(parsed_us)
                        us_cash_live = float(us_cash)
                        result["us_ok"] = True
            else:
                snap = load_last_kis_display_snapshot()
                us_d = snap.get("us") or {}
                if isinstance(us_d, dict) and us_d.get("total") is not None:
                    total_us_equity = float(us_d["total"])
                    result["us_ok"] = True
                print("  📌 [circuit_aux 갱신] 미장 비장중 — last_kis_display_snapshot 사용")
        except Exception as e:
            print(f"  ⚠️ [circuit_aux 갱신] 미장 조회 실패: {e}")

    try:
        balances = coin_broker.get_balances() or []
        if coin_config.is_binance():
            kpx = float(coin_broker.get_krw_per_usdt())
            usdt_row = next((b for b in balances if str(b.get("currency", "")).upper() == "USDT"), None) or {}
            usdt_total = _to_float(usdt_row.get("balance", 0), 0.0)
            total_coin_equity = float(usdt_total * kpx)
            for b in balances:
                if str(b.get("currency", "")).upper() in ("USDT", "VTHO"):
                    continue
                t = coin_broker.held_ticker_row(b)
                if not t:
                    continue
                curr_p = coin_broker.get_current_price(t)
                if curr_p:
                    total_coin_equity += float(_to_float(b.get("balance", 0))) * float(curr_p) * kpx
        else:
            krw_row = next((b for b in balances if str(b.get("currency", "")).upper() == "KRW"), None) or {}
            krw_on_book = _to_float(krw_row.get("balance", 0), 0.0)
            total_coin_equity = float(krw_on_book)
            for b in balances:
                if b.get("currency") in ("KRW", "VTHO"):
                    continue
                t = coin_broker.held_ticker_row(b)
                if not t:
                    continue
                curr_p = coin_broker.get_current_price(t)
                if curr_p:
                    total_coin_equity += float(_to_float(b.get("balance", 0))) * float(curr_p)
        result["coin_ok"] = True
    except Exception as e:
        print(f"  ⚠️ [circuit_aux 갱신] 코인 조회 실패: {e}")

    # 장외에는 KR/US를 표시 스냅샷 기준으로 고정해 불안정한 야간 KIS 응답을 차단.
    try:
        disp = load_last_kis_display_snapshot()
        kr_d = disp.get("kr") if isinstance(disp.get("kr"), dict) else {}
        us_d = disp.get("us") if isinstance(disp.get("us"), dict) else {}
        snap_kr_total = float(_to_float(kr_d.get("total", 0), 0.0))
        snap_us_total = float(_to_float(us_d.get("total", 0), 0.0))
        if (not is_market_open("KR")) and snap_kr_total > 0:
            total_kr_equity = snap_kr_total
            result["kr_ok"] = True
        if (not is_market_open("US")) and snap_us_total > 0:
            total_us_equity = snap_us_total
            result["us_ok"] = True
    except Exception:
        pass

    if result["kr_ok"]:
        kr_part = lv._kis_snap_bucket(state, "KR")
        cash_kr = float(kr_cash_live) if kr_cash_live > 0 else float(kr_part.get("cash", 0) or 0)
        lv.write_kis_display_snapshot_part(
            state, "KR", cash=cash_kr, total=float(total_kr_equity)
        )
    if result["us_ok"]:
        us_part = lv._kis_snap_bucket(state, "US")
        cash_us = float(us_cash_live) if us_cash_live > 0 else float(us_part.get("cash", 0) or 0)
        lv.write_kis_display_snapshot_part(
            state, "US", cash=cash_us, total=float(total_us_equity)
        )
    if result["coin_ok"]:
        state["circuit_aux_last_coin_krw"] = float(total_coin_equity)
    save_state(path, state)
    result["totals"] = {
        "kr_krw": float(total_kr_equity),
        "usd_total": float(total_us_equity),
        "coin_krw": float(total_coin_equity),
    }
    return result


from execution import phase5_ops as _phase5_ops

_phase5_liquidate_market = _phase5_ops.liquidate_market
_phase5_emergency_liquidate_all = _phase5_ops.emergency_liquidate_all
_phase5_market_has_ledger_positions = _phase5_ops.market_has_ledger_positions
_phase5_pending_markets = _phase5_ops.pending_markets
_phase5_migrate_legacy_pending_flag = _phase5_ops.migrate_legacy_pending_flag
_phase5_prune_stale_pending = _phase5_ops.prune_stale_pending
_phase5_set_pending_markets = _phase5_ops.set_pending_markets
_phase5_try_pending_liquidation = _phase5_ops.try_pending_liquidation
_maybe_run_account_circuit = _phase5_ops.maybe_run_account_circuit


def _phase5_pending_positions_exist(state: dict) -> bool:
    """장부 기준 미청산 포지션 존재 여부."""
    pos = state.get("positions", {}) if isinstance(state, dict) else {}
    return isinstance(pos, dict) and len(pos) > 0


def _calc_kr_holdings_metrics(balance_data):
    """국내 포지션 지표 — ``api.kis_parsers.parse_kr_holdings_metrics`` 위임."""
    return parse_kr_holdings_metrics(balance_data, _to_float)


def _calc_us_holdings_metrics(balance_data):
    """미국 포지션 지표 — ``api.kis_parsers.parse_us_holdings_metrics`` 위임."""
    return parse_us_holdings_metrics(balance_data, _to_float)

def _calc_coin_holdings_metrics(balances, positions=None):
    """코인 포지션 지표. 바이낸스는 avg_buy_price가 없어 장부·매매내역 평단을 쓴다."""
    if not balances:
        return {"invested": 0.0, "current": 0.0, "profit": 0.0, "roi": 0.0}
    if positions is None:
        try:
            positions = load_state(STATE_PATH).get("positions", {}) or {}
        except Exception:
            positions = {}
    if not isinstance(positions, dict):
        positions = {}
    try:
        total_invested = 0.0
        total_current = 0.0
        kpx = float(coin_broker.get_krw_per_usdt()) if coin_config.is_binance() else 1.0
        for b in balances:
            if b['currency'] in ('KRW', 'VTHO'):
                continue
            if coin_config.is_binance() and str(b.get("currency", "")).upper() == "USDT":
                continue
            qty = _to_float(b.get('balance', 0))
            if not coin_broker.should_include_coin_balance_row(b):
                continue
            ticker = coin_broker.held_ticker_row(b)
            if not ticker:
                continue
            curr_price = float(coin_broker.get_current_price(ticker) or 0)
            if coin_config.is_binance():
                current = qty * curr_price * kpx
            else:
                current = qty * curr_price
            total_current += current

            avg_buy_price = _to_float(b.get('avg_buy_price', 0))
            if avg_buy_price <= 0:
                pos = positions.get(ticker) if isinstance(positions.get(ticker), dict) else {}
                avg_buy_price = _to_float(pos.get("buy_p", 0), 0.0)
            if avg_buy_price <= 0:
                avg_buy_price = float(_last_buy_price_from_trade_history(ticker, "COIN") or 0.0)
            if avg_buy_price > 0:
                if coin_config.is_binance():
                    invested = qty * avg_buy_price * kpx
                else:
                    invested = qty * avg_buy_price
                total_invested += invested

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

    if not isinstance(position_payload, dict):
        print(f"  ❌ [{context}] 장부 등록 실패: payload 타입 오류")
        return False
    position_payload = dict(position_payload)
    position_payload.setdefault("scale_out_done", False)
    position_payload.setdefault("second_scale_out_done", False)
    if "entry_atr" not in position_payload:
        position_payload["entry_atr"] = float(_to_float(position_payload.get("current_atr", 0), 0.0))
    existing = state.get("positions", {}).get(ticker)
    if isinstance(existing, dict) and "BUY" in str(context or "").upper():
        try:
            old_bt = float(_to_float(existing.get("buy_time", 0), 0.0))
            new_bt = float(_to_float(position_payload.get("buy_time", 0), 0.0))
            old_qty = float(_to_float(existing.get("qty", 0), 0.0))
            new_qty = float(_to_float(position_payload.get("qty", 0), 0.0))
            if abs(new_bt - old_bt) < 120.0 and new_qty <= old_qty * 1.02:
                print(f"  ✅ [{context}] 장부 이미 등록됨(멱등): {ticker}")
                return True
            if new_qty > old_qty:
                position_payload["qty"] = new_qty
        except (TypeError, ValueError):
            pass
    def _mut_buy(st: dict) -> None:
        set_cooldown(st, ticker)

    return ledger_apply.persist_position_set(
        state,
        ticker,
        position_payload,
        context=context or "장부 등록",
        state_path=state_path,
        mutate_fn=_mut_buy,
    )


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


def _kis_balance_qty_for_ticker(
    market: str, ticker: str, *, refresh: bool = False
) -> float | None:
    try:
        return bal_read.stock_qty(market, ticker, refresh=refresh)
    except Exception:
        return None


def _coin_balance_qty_for_ticker(ticker: str, *, refresh: bool = False) -> float | None:
    try:
        return bal_read.coin_stock_qty(ticker, refresh=refresh)
    except Exception:
        return None


def _idempotent_kis_sell(
    state: dict,
    *,
    market: str,
    ticker: str,
    lane: str,
    qty: int,
    fallback_price: float,
    place_order,
    slice_index: int = 0,
    cycle_tag: str | None = None,
    use_inflight: bool = True,
) -> order_idem.SliceFillResult:
    """KIS 매도 1회(또는 Scale-Out 슬라이스) — 멱등·선택적 sell_inflight."""
    ct = cycle_tag or order_idem.cycle_tag_15m_kst()
    acquired = True
    if use_inflight:
        acquired = order_idem.try_acquire_sell_inflight(state, market, ticker, lane, ct)
        if not acquired:
            return order_idem.SliceFillResult(False, 0.0, 0.0, note="매도 진행 중(멱등)")
    qty_before = None
    bal_fn = None
    if not TEST_MODE:
        try:
            qty_before = _kis_balance_qty_for_ticker(market, ticker, refresh=False)
        except Exception:
            qty_before = None

        def _bal():
            return _kis_balance_qty_for_ticker(market, ticker, refresh=True)

        bal_fn = _bal
    try:
        fill = order_idem.run_kis_sell_slice_idempotent(
            state,
            market=market,
            ticker=ticker,
            lane=lane,
            slice_index=int(slice_index),
            qty=int(qty),
            cycle_tag=ct,
            place_order=place_order,
            fallback_price=float(fallback_price),
            balance_qty_fn=bal_fn,
            qty_before=qty_before,
            test_mode=TEST_MODE,
        )
        if not fill.ok and not TEST_MODE:
            order_idem.persist_idempotency(state, STATE_PATH)
            bal_read.invalidate(market)
        elif fill.ok and not TEST_MODE:
            bal_read.invalidate(market)
            from execution.balance_policy import mark_balance_live_sync

            mark_balance_live_sync(state, STATE_PATH)
        return fill
    finally:
        if use_inflight and acquired:
            order_idem.release_sell_inflight(state, market, ticker, lane, ct)


def _run_kis_scale_out_slices_idempotent(
    state: dict,
    *,
    market: str,
    ticker: str,
    sell_qty: int,
    notional_krw: float,
    threshold_krw: float,
    curr_p: float,
    place_slice,
    cycle_tag: str | None = None,
    lane: str | None = None,
    log_label: str = "Scale-Out",
) -> bool:
    """V8 Scale-Out — 슬라이스별 멱등 매도( sell_inflight 없음 )."""
    sell_lane = lane or order_idem.LANE_SCALE_OUT
    chunks = plan_sell_qty_twap(int(sell_qty), float(notional_krw), threshold_krw=float(threshold_krw))
    ct = cycle_tag or order_idem.cycle_tag_15m_kst()
    for si, qq in enumerate(chunks):
        if int(qq) <= 0:
            continue

        def _place():
            return place_slice(int(qq))

        fill = _idempotent_kis_sell(
            state,
            market=market,
            ticker=ticker,
            lane=sell_lane,
            qty=int(qq),
            fallback_price=float(curr_p),
            place_order=_place,
            slice_index=si,
            cycle_tag=ct,
            use_inflight=False,
        )
        tag = "♻️" if fill.reused else "🧾"
        print(
            f"  {tag} [{market} {log_label} {si + 1}/{len(chunks)}] {ticker} "
            f"ok={fill.ok} qty={int(fill.qty)} note={fill.note}"
        )
        if not fill.ok:
            return False
        if TWAP_SLICE_DELAY_SEC > 0 and si < len(chunks) - 1:
            time.sleep(TWAP_SLICE_DELAY_SEC)
    return True


def _idempotent_coin_sell(
    state: dict,
    *,
    ticker: str,
    lane: str,
    qty: float,
    fallback_price: float,
    slice_index: int = 0,
    cycle_tag: str | None = None,
    use_inflight: bool = True,
) -> order_idem.SliceFillResult:
    """코인 매도 — 바이낸스 clientOrderId / 업비트 잔고 검증."""
    ct = cycle_tag or order_idem.cycle_tag_15m_kst()
    acquired = True
    if use_inflight:
        acquired = order_idem.try_acquire_sell_inflight(state, "COIN", ticker, lane, ct)
        if not acquired:
            return order_idem.SliceFillResult(False, 0.0, 0.0, note="매도 진행 중(멱등)")
    qty_before = None
    bal_fn = None
    if not TEST_MODE:
        try:
            qty_before = _coin_balance_qty_for_ticker(ticker, refresh=False)
        except Exception:
            qty_before = None

        def _bal():
            return _coin_balance_qty_for_ticker(ticker, refresh=True)

        bal_fn = _bal
    try:
        if coin_config.is_binance():

            def _bn_place(cid: str):
                return coin_broker.sell_market(
                    ticker, float(qty), new_client_order_id=cid
                )

            fill = order_idem.run_binance_sell_idempotent(
                state,
                market="COIN",
                ticker=ticker,
                lane=lane,
                slice_index=int(slice_index),
                cycle_tag=ct,
                qty=float(qty),
                place_order=_bn_place,
                fallback_price=float(fallback_price),
                balance_qty_fn=bal_fn,
                qty_before=qty_before,
                test_mode=TEST_MODE,
            )
        else:

            def _up_place():
                if upbit_api.upbit is None:
                    return None
                return upbit_api.upbit.sell_market_order(ticker, float(qty))

            fill = order_idem.run_upbit_sell_slice_idempotent(
                state,
                market="COIN",
                ticker=ticker,
                lane=lane,
                slice_index=int(slice_index),
                cycle_tag=ct,
                qty=float(qty),
                place_order=_up_place,
                fallback_price=float(fallback_price),
                balance_qty_fn=bal_fn,
                qty_before=qty_before,
                test_mode=TEST_MODE,
            )
        if not fill.ok and not TEST_MODE:
            order_idem.persist_idempotency(state, STATE_PATH)
            bal_read.invalidate("COIN")
        elif fill.ok and not TEST_MODE:
            bal_read.invalidate("COIN")
        return fill
    finally:
        if use_inflight and acquired:
            order_idem.release_sell_inflight(state, "COIN", ticker, lane, ct)


def _run_coin_scale_out_slices_idempotent(
    state: dict,
    *,
    ticker: str,
    chunks: list[float],
    curr_p: float,
    cycle_tag: str | None = None,
    lane: str | None = None,
    log_label: str = "Scale-Out",
) -> bool:
    """코인 Scale-Out — 덩어리별 멱등 매도."""
    sell_lane = lane or order_idem.LANE_SCALE_OUT
    ct = cycle_tag or order_idem.cycle_tag_15m_kst()
    flist = [truncate_coin_qty(float(c)) for c in chunks]
    flist = [x for x in flist if x > 0]
    if not flist:
        return False
    for si, vv in enumerate(flist):
        fill = _idempotent_coin_sell(
            state,
            ticker=ticker,
            lane=sell_lane,
            qty=float(vv),
            fallback_price=float(curr_p),
            slice_index=si,
            cycle_tag=ct,
            use_inflight=False,
        )
        tag = "♻️" if fill.reused else "🧾"
        print(
            f"  {tag} [COIN {log_label} {si + 1}/{len(flist)}] {ticker} "
            f"ok={fill.ok} qty={fill.qty:.6f} note={fill.note}"
        )
        if not fill.ok:
            return False
        if TWAP_SLICE_DELAY_SEC > 0 and si < len(flist) - 1:
            time.sleep(TWAP_SLICE_DELAY_SEC)
    return True


def _try_v8_scale_out_kr_us(
    state: dict,
    *,
    market: str,
    ticker: str,
    pos_info: dict,
    qty: int,
    buy_p: float,
    curr_p: float,
    profit_rate_now: float,
    cycle_tag: str,
    is_us: bool,
    place_slice,
    display_name: str,
) -> tuple[bool, dict]:
    """
    V8(TREND_V8) 분할 익절 — 1차 ``entry_atr×3`` · 2차 ``entry_atr×6`` (1차 후 잔량 50%).

    Returns:
        (handled_continue, pos_info) — True면 매도 루프 ``continue``.
    """
    mkt = str(market or "").strip().upper()
    pos = dict(pos_info) if isinstance(pos_info, dict) else {}
    usdk = float(estimate_usdkrw())
    qty_int = int(qty)
    q_led = int(round(_to_float(pos.get("qty"), qty_int)))
    if q_led <= 0:
        q_led = qty_int
    notion_krw_so = notional_krw_kr_us(float(buy_p), float(curr_p), float(q_led), is_us, usdk)
    entry_atr = _to_float(pos.get("entry_atr", 0), 0.0)

    # --- 1차 Scale-Out (3×ATR) ---
    so_hit, so_mode, so_target = scale_out_price_target_hit(
        float(buy_p), float(curr_p), entry_atr, atr_mult=SCALE_OUT_ENTRY_ATR_MULT
    )
    if order_idem.lane_has_filled_sell(state, mkt, ticker, order_idem.LANE_SCALE_OUT, cycle_tag):
        order_idem.reconcile_ticker_lane(
            state, mkt, ticker, order_idem.LANE_SCALE_OUT, cycle_tag, STATE_PATH
        )
        pos = (state.get("positions") or {}).get(ticker) or pos
    if not position_scale_out_done(pos) and so_hit:
        if float(notion_krw_so) < SCALE_OUT_MIN_NOTIONAL_KRW:
            mode_txt = (
                f"entry_atr*{SCALE_OUT_ENTRY_ATR_MULT:.1f}"
                if so_mode == "entry_atr"
                else f"fallback +{SCALE_OUT_PROFIT_PCT:.0f}%"
            )
            print(
                f"  ℹ️ [{mkt} Scale-Out 1차 스킵] {ticker}: 트리거({mode_txt}, 목표 {so_target:,.0f})는 충족했지만 "
                f"명목={notion_krw_so:,.0f}원 < {SCALE_OUT_MIN_NOTIONAL_KRW:,.0f}원"
            )
        elif scale_out_trigger_ok(pos, SCALE_OUT_PROFIT_PCT, notion_krw_so):
            sq = compute_stock_scale_out_qty(qty_int)
            if not sq:
                print(
                    f"  ℹ️ [{mkt} Scale-Out 1차 스킵] {ticker}: 보유 {qty_int}주 → 50% 몫 0주"
                )
            elif not stock_scale_out_min_notional_ok(int(sq), float(curr_p)):
                print(f"  ℹ️ [{mkt} Scale-Out 1차 스킵] {ticker}: 최소 매도 명목 미만")
            else:
                sell_notion_krw = float(sq) * float(curr_p) * (usdk if is_us else 1.0)
                tw_krw = (
                    (float(TWAP_USD_THRESHOLD) * usdk)
                    if is_us and TWAP_ENABLED
                    else (TWAP_KRW_THRESHOLD if TWAP_ENABLED else float("inf"))
                )
                ok_so = _run_kis_scale_out_slices_idempotent(
                    state,
                    market=mkt,
                    ticker=ticker,
                    sell_qty=int(sq),
                    notional_krw=sell_notion_krw,
                    threshold_krw=tw_krw,
                    curr_p=float(curr_p),
                    place_slice=place_slice,
                    cycle_tag=cycle_tag,
                    lane=order_idem.LANE_SCALE_OUT,
                    log_label="Scale-Out 1차",
                )
                if ok_so:
                    ledger_apply.persist_position_set(
                        state,
                        ticker,
                        post_partial_ledger(pos, float(sq), float(curr_p), float(qty_int)),
                        context=f"{mkt} Scale-Out 1차",
                        state_path=STATE_PATH,
                    )
                    try:
                        _record_trade_event(
                            mkt,
                            ticker,
                            "SELL",
                            int(sq),
                            price=float(curr_p),
                            profit_rate=float(profit_rate_now),
                            reason="V7.1 조건부 50% 분할 익절(Scale-Out 1차)",
                        )
                    except Exception as _e_so:
                        print(f"  ⚠️ [{mkt} Scale-Out 1차] 매매내역 기록 실패: {_e_so}")
                    print(
                        f"  ✅ [{mkt} Scale-Out 1차] {display_name}({ticker}) {sq}주 분할 익절 · 장부 보정 완료"
                    )
                    send_telegram(
                        f"💎 [{mkt} Scale-Out 1차] {ticker}({display_name})\n"
                        f"{sq}주 분할 익절 체결, 남은 물량은 샹들리에 추적 유지"
                    )
                    return True, (state.get("positions") or {}).get(ticker) or pos
                print(f"  ⚠️ [{mkt} Scale-Out 1차] {ticker} 주문 실패 — 다음 사이클에 재시도")

    pos = (state.get("positions") or {}).get(ticker) or pos
    qty_rem = int(round(_to_float(pos.get("qty"), 0)))
    if qty_rem <= 0:
        return False, pos
    notion_rem = notional_krw_kr_us(float(buy_p), float(curr_p), float(qty_rem), is_us, usdk)

    # --- 2차 Scale-Out (6×ATR, 1차 완료 후) ---
    so2_ok, so2_mode, so2_target = scale_out_second_trigger_ok(
        pos, float(buy_p), float(curr_p), entry_atr, notion_rem
    )
    if order_idem.lane_has_filled_sell(
        state, mkt, ticker, order_idem.LANE_SCALE_OUT_2, cycle_tag
    ):
        order_idem.reconcile_ticker_lane(
            state, mkt, ticker, order_idem.LANE_SCALE_OUT_2, cycle_tag, STATE_PATH
        )
        pos = (state.get("positions") or {}).get(ticker) or pos
    if so2_ok:
        sq2 = compute_stock_scale_out_qty(qty_rem)
        if not sq2:
            print(f"  ℹ️ [{mkt} Scale-Out 2차 스킵] {ticker}: 잔량 {qty_rem}주 → 50% 몫 0주")
        elif not stock_scale_out_min_notional_ok(int(sq2), float(curr_p)):
            print(f"  ℹ️ [{mkt} Scale-Out 2차 스킵] {ticker}: 최소 매도 명목 미만")
        else:
            sell_notion_krw = float(sq2) * float(curr_p) * (usdk if is_us else 1.0)
            tw_krw = (
                (float(TWAP_USD_THRESHOLD) * usdk)
                if is_us and TWAP_ENABLED
                else (TWAP_KRW_THRESHOLD if TWAP_ENABLED else float("inf"))
            )
            mode_txt = (
                f"entry_atr*{SCALE_OUT_SECOND_ENTRY_ATR_MULT:.1f}"
                if so2_mode == "entry_atr"
                else f"fallback"
            )

            def _place2():
                return place_slice(int(sq2))

            ok_so2 = _run_kis_scale_out_slices_idempotent(
                state,
                market=mkt,
                ticker=ticker,
                sell_qty=int(sq2),
                notional_krw=sell_notion_krw,
                threshold_krw=tw_krw,
                curr_p=float(curr_p),
                place_slice=_place2,
                cycle_tag=cycle_tag,
                lane=order_idem.LANE_SCALE_OUT_2,
                log_label="Scale-Out 2차",
            )
            if ok_so2:
                ledger_apply.persist_position_set(
                    state,
                    ticker,
                    post_partial_ledger(
                        pos,
                        float(sq2),
                        float(curr_p),
                        float(qty_rem),
                        set_scale_out_done=False,
                        set_second_scale_out_done=True,
                    ),
                    context=f"{mkt} Scale-Out 2차",
                    state_path=STATE_PATH,
                )
                try:
                    _record_trade_event(
                        mkt,
                        ticker,
                        "SELL",
                        int(sq2),
                        price=float(curr_p),
                        profit_rate=float(profit_rate_now),
                        reason=f"V8 2차 분할 익절(Scale-Out, {mode_txt}, 목표 {so2_target:,.2f})",
                    )
                except Exception as _e_so2:
                    print(f"  ⚠️ [{mkt} Scale-Out 2차] 매매내역 기록 실패: {_e_so2}")
                print(
                    f"  ✅ [{mkt} Scale-Out 2차] {display_name}({ticker}) {sq2}주 분할 익절 · 장부 보정 완료"
                )
                send_telegram(
                    f"💎 [{mkt} Scale-Out 2차] {ticker}({display_name})\n"
                    f"{sq2}주 2차 분할 익절({mode_txt})"
                )
                return True, (state.get("positions") or {}).get(ticker) or pos
            print(f"  ⚠️ [{mkt} Scale-Out 2차] {ticker} 주문 실패 — 다음 사이클에 재시도")
    elif position_scale_out_done(pos) and not position_second_scale_out_done(pos):
        if float(notion_rem) < SCALE_OUT_MIN_NOTIONAL_KRW and so2_target > 0:
            print(
                f"  ℹ️ [{mkt} Scale-Out 2차 스킵] {ticker}: 6×ATR 목표 {so2_target:,.0f} 충족 전 명목 부족"
            )

    return False, pos


def _try_v8_scale_out_coin(
    state: dict,
    *,
    ticker: str,
    pos_info: dict,
    qty: float,
    buy_p: float,
    curr_p: float,
    profit_rate_now: float,
    cycle_tag: str,
) -> tuple[bool, dict]:
    """V8 코인 분할 익절 1차·2차. (handled_continue, pos_info)."""
    pos = dict(pos_info) if isinstance(pos_info, dict) else {}
    usdk = float(estimate_usdkrw())
    q_led = float(_to_float(pos.get("qty"), qty))
    if q_led <= 0:
        q_led = float(qty)
    notion_krw_so = notional_krw_kr_us(
        float(buy_p), float(curr_p), float(q_led), bool(coin_config.is_binance()), usdk
    )
    entry_atr = _to_float(pos.get("entry_atr", 0), 0.0)

    so_hit, so_mode, so_target = scale_out_price_target_hit(
        float(buy_p), float(curr_p), entry_atr, atr_mult=SCALE_OUT_ENTRY_ATR_MULT
    )
    if order_idem.lane_has_filled_sell(state, "COIN", ticker, order_idem.LANE_SCALE_OUT, cycle_tag):
        order_idem.reconcile_ticker_lane(
            state, "COIN", ticker, order_idem.LANE_SCALE_OUT, cycle_tag, STATE_PATH
        )
        pos = (state.get("positions") or {}).get(ticker) or pos
    if not position_scale_out_done(pos) and so_hit:
        if float(notion_krw_so) < SCALE_OUT_MIN_NOTIONAL_KRW:
            mode_txt = (
                f"entry_atr*{SCALE_OUT_ENTRY_ATR_MULT:.1f}"
                if so_mode == "entry_atr"
                else f"fallback +{SCALE_OUT_PROFIT_PCT:.0f}%"
            )
            print(
                f"  ℹ️ [COIN Scale-Out 1차 스킵] {ticker}: 트리거({mode_txt}, 목표 {so_target:,.0f}) "
                f"명목={notion_krw_so:,.0f}원 < {SCALE_OUT_MIN_NOTIONAL_KRW:,.0f}원"
            )
        elif scale_out_trigger_ok(pos, SCALE_OUT_PROFIT_PCT, notion_krw_so):
            sell_q = compute_coin_scale_out_qty(float(qty), float(curr_p))
            if not sell_q:
                print(f"  ℹ️ [COIN Scale-Out 1차 스킵] {ticker}: 50% 절삼 후 수량 0")
            elif not coin_broker.scale_out_min_notional_ok(float(sell_q), float(curr_p)):
                print(f"  ℹ️ [COIN Scale-Out 1차 스킵] {ticker}: 거래소 최소 명목 미만")
            else:
                tw_th = TWAP_KRW_THRESHOLD if TWAP_ENABLED else float("inf")
                chunks = plan_coin_sell_chunks(float(sell_q), float(curr_p), threshold_krw=float(tw_th))
                ok_so = _run_coin_scale_out_slices_idempotent(
                    state,
                    ticker=ticker,
                    chunks=chunks,
                    curr_p=float(curr_p),
                    cycle_tag=cycle_tag,
                    lane=order_idem.LANE_SCALE_OUT,
                    log_label="Scale-Out 1차",
                )
                if ok_so:
                    ledger_apply.persist_position_set(
                        state,
                        ticker,
                        post_partial_ledger(pos, float(sell_q), float(curr_p), float(qty)),
                        context="COIN Scale-Out 1차",
                        state_path=STATE_PATH,
                    )
                    try:
                        _record_trade_event(
                            "COIN",
                            ticker,
                            "SELL",
                            float(sell_q),
                            price=float(curr_p),
                            profit_rate=float(profit_rate_now),
                            reason="V7.1 조건부 50% 분할 익절(Scale-Out 1차)",
                        )
                    except Exception as _e_so:
                        print(f"  ⚠️ [COIN Scale-Out 1차] 매매내역 기록 실패: {_e_so}")
                    cn = get_coin_name(ticker)
                    print(f"  ✅ [COIN Scale-Out 1차] {ticker}({cn}) 분할 익절 {sell_q} · 장부 보정 완료")
                    send_telegram(
                        f"💎 [COIN Scale-Out 1차] {ticker}({cn})\n분할 익절 체결, 남은 물량은 샹들리에 추적 유지"
                    )
                    return True, (state.get("positions") or {}).get(ticker) or pos
                print(f"  ⚠️ [COIN Scale-Out 1차] {ticker} 주문 실패 — 다음 사이클에 재시도")

    pos = (state.get("positions") or {}).get(ticker) or pos
    qty_rem = float(_to_float(pos.get("qty"), 0))
    if qty_rem <= 0:
        return False, pos
    notion_rem = notional_krw_kr_us(
        float(buy_p), float(curr_p), qty_rem, bool(coin_config.is_binance()), usdk
    )
    so2_ok, so2_mode, so2_target = scale_out_second_trigger_ok(
        pos, float(buy_p), float(curr_p), entry_atr, notion_rem
    )
    if order_idem.lane_has_filled_sell(state, "COIN", ticker, order_idem.LANE_SCALE_OUT_2, cycle_tag):
        order_idem.reconcile_ticker_lane(
            state, "COIN", ticker, order_idem.LANE_SCALE_OUT_2, cycle_tag, STATE_PATH
        )
        pos = (state.get("positions") or {}).get(ticker) or pos
    if so2_ok:
        sell_q2 = compute_coin_scale_out_qty(qty_rem, float(curr_p))
        if not sell_q2:
            print(f"  ℹ️ [COIN Scale-Out 2차 스킵] {ticker}: 50% 절삼 후 수량 0")
        elif not coin_broker.scale_out_min_notional_ok(float(sell_q2), float(curr_p)):
            print(f"  ℹ️ [COIN Scale-Out 2차 스킵] {ticker}: 거래소 최소 명목 미만")
        else:
            tw_th = TWAP_KRW_THRESHOLD if TWAP_ENABLED else float("inf")
            chunks2 = plan_coin_sell_chunks(float(sell_q2), float(curr_p), threshold_krw=float(tw_th))
            ok_so2 = _run_coin_scale_out_slices_idempotent(
                state,
                ticker=ticker,
                chunks=chunks2,
                curr_p=float(curr_p),
                cycle_tag=cycle_tag,
                lane=order_idem.LANE_SCALE_OUT_2,
                log_label="Scale-Out 2차",
            )
            if ok_so2:
                ledger_apply.persist_position_set(
                    state,
                    ticker,
                    post_partial_ledger(
                        pos,
                        float(sell_q2),
                        float(curr_p),
                        qty_rem,
                        set_scale_out_done=False,
                        set_second_scale_out_done=True,
                    ),
                    context="COIN Scale-Out 2차",
                    state_path=STATE_PATH,
                )
                try:
                    _record_trade_event(
                        "COIN",
                        ticker,
                        "SELL",
                        float(sell_q2),
                        price=float(curr_p),
                        profit_rate=float(profit_rate_now),
                        reason=f"V8 2차 분할 익절(Scale-Out, entry_atr×{SCALE_OUT_SECOND_ENTRY_ATR_MULT:.1f})",
                    )
                except Exception as _e_so2:
                    print(f"  ⚠️ [COIN Scale-Out 2차] 매매내역 기록 실패: {_e_so2}")
                cn = get_coin_name(ticker)
                print(f"  ✅ [COIN Scale-Out 2차] {ticker}({cn}) 2차 분할 익절 {sell_q2}")
                send_telegram(f"💎 [COIN Scale-Out 2차] {ticker}({cn})\n2차 분할 익절 체결")
                return True, (state.get("positions") or {}).get(ticker) or pos
            print(f"  ⚠️ [COIN Scale-Out 2차] {ticker} 주문 실패 — 다음 사이클에 재시도")

    return False, pos



def _twap_krw_budget_slices(total_krw: float) -> list:
    from execution import order_executor as oe
    return oe.twap_krw_budget_slices(total_krw)


def _twap_usd_budget_slices(total_usd: float) -> list:
    from execution import order_executor as oe
    return oe.twap_usd_budget_slices(total_usd)


def _execute_kr_market_buy_twap(
    t: str,
    kr_name: str,
    target_budget: float,
    curr_p: float,
    sl_p: float,
    entry_atr: float,
    t_name: str,
    s_name: str,
    state: dict,
    kr_cash_holder: list,
    *,
    strategy_type: str = "TREND_V8",
    entry_fib_level: float = 0.0,
) -> bool:
    from execution import order_executor as oe
    return oe.execute_kr_market_buy_twap(
        t,
        kr_name,
        target_budget,
        curr_p,
        sl_p,
        entry_atr,
        t_name,
        s_name,
        state,
        kr_cash_holder,
        strategy_type=strategy_type,
        entry_fib_level=entry_fib_level,
    )


def _execute_us_market_buy_twap(
    t: str,
    us_name: str,
    target_budget_usd: float,
    curr_p: float,
    sl_p: float,
    entry_atr: float,
    t_name: str,
    s_name: str,
    state: dict,
    us_cash_holder: list,
    *,
    strategy_type: str = "TREND_V8",
    entry_fib_level: float = 0.0,
) -> bool:
    from execution import order_executor as oe
    return oe.execute_us_market_buy_twap(
        t,
        us_name,
        target_budget_usd,
        curr_p,
        sl_p,
        entry_atr,
        t_name,
        s_name,
        state,
        us_cash_holder,
        strategy_type=strategy_type,
        entry_fib_level=entry_fib_level,
    )


def _execute_coin_market_buy_twap(
    t: str,
    budget_krw: float,
    sl_p: float,
    entry_atr: float,
    s_name: str,
    state: dict,
    krw_bal_holder: list,
    held_coins_mut: list,
    *,
    strategy_type: str = "TREND_V8",
    entry_fib_level: float = 0.0,
) -> bool:
    from execution import order_executor as oe
    return oe.execute_coin_market_buy_twap(
        t,
        budget_krw,
        sl_p,
        entry_atr,
        s_name,
        state,
        krw_bal_holder,
        held_coins_mut,
        strategy_type=strategy_type,
        entry_fib_level=entry_fib_level,
    )


def _holding_duration_human(pos: dict, market: str = "") -> str:
    """
    장부 매수 시각 기준 보유 시간 (텔레그램·GUI·타임스탑과 동일).

    KR/US: 거래일 기준 24시간(주말 제외) · COIN: 24/7 연속 — 모두 ``N.Nh`` 만 표기.
    """
    if not isinstance(pos, dict):
        return ""
    buy_dt = _position_buy_anchor_dt(pos)
    if buy_dt is None:
        return ""
    now = datetime.now()
    m = str(market or "").strip().upper()
    if m in ("KR", "US", "COIN"):
        try:
            th = _time_stop_hours_elapsed(m, buy_dt, now)
            return f"{float(th):.1f}h"
        except Exception:
            pass
    try:
        delta_sec = max(0.0, (now - buy_dt).total_seconds())
        return f"{delta_sec / 3600.0:.1f}h"
    except Exception:
        return ""


def _holding_duration_suffix(pos: dict, market: str = "") -> str:
    d = _holding_duration_human(pos, market)
    return f" | 보유 {d}" if d else ""


def _holding_duration_clause(pos: dict, market: str = "") -> str:
    """생존신고 보유 한 줄 접미사 — 타임스탑과 동일한 누적 시간(N.Nh)."""
    if not isinstance(pos, dict):
        return ""
    d = _holding_duration_human(pos, market)
    return f" | 보유 {d}" if d else ""


def _fmt_telegram_coin_unit_usdt(p: float) -> str:
    """텔레그램·로그: 바이낸스 코인 단가(USDT) — GUI ``_gui_coin_unit_price_str`` 와 동일 룰."""
    x = float(_to_float(p, 0.0))
    if x >= 1000:
        return f"{x:,.2f} USDT"
    if x >= 1:
        return f"{x:,.4f} USDT"
    if x <= 0:
        return "0 USDT"
    s = f"{x:.8f}".rstrip("0").rstrip(".")
    return f"{s} USDT" if s else "0 USDT"


def _strategy_from_trade_history_buy(ticker: str, market: str) -> str | None:
    """``trade_history.json`` 최근 BUY ``reason`` 으로 V8/스윙 추론."""
    t_key = str(ticker or "").strip()
    if not t_key:
        return None
    m = str(market or "").strip().upper()
    try:
        if not TRADE_HISTORY_PATH.exists():
            return None
        import json

        with open(TRADE_HISTORY_PATH, encoding="utf-8") as f:
            rows = json.load(f)
        if not isinstance(rows, list):
            return None
        t_norm = normalize_ticker(t_key) if not is_coin_ticker(t_key) else t_key.upper()
        for row in reversed(rows):
            if not isinstance(row, dict):
                continue
            if str(row.get("side", "")).upper() != "BUY":
                continue
            rt = str(row.get("ticker", "")).strip()
            rt_cmp = rt.upper() if is_coin_ticker(rt) else normalize_ticker(rt)
            if rt_cmp != t_norm and rt.upper() != t_key.upper():
                continue
            rm = str(row.get("market", "")).strip().upper()
            if m and rm and rm != m:
                continue
            reason = str(row.get("reason", "") or "")
            ru = reason.upper()
            if "SWING" in ru or reason.strip() == "SWING_FIB":
                return "스윙"
            if reason.strip():
                return "V8"
        return None
    except Exception:
        return None


def _resolve_sell_loop_strategy_type(pos_info: dict) -> str:
    """
    매도 루프용 ``strategy_type`` — ``tier``·``strategy_type`` 동시 판별.

    ``strategy_type`` 미기록·기본 ``TREND_V8`` 인 SWING_FIB 보유가
    V8 분할 익절(Scale-Out, ``entry_atr×3.0``) 블록에 들어가는 침범을 막습니다.
    """
    p = pos_info if isinstance(pos_info, dict) else {}
    st = str(p.get("strategy_type") or "").strip().upper()
    tier = str(p.get("tier") or "").strip().upper()
    if st == "SWING_FIB" or tier in ("SWING_FIB", "SWING") or "SWING" in tier:
        return "SWING_FIB"
    if st:
        return st
    return "TREND_V8"


def _heartbeat_strategy_label(
    pos: dict, *, ticker: str = "", market: str = ""
) -> str:
    """생존신고 보유 한 줄 — 매수 전략 표시."""
    p = pos if isinstance(pos, dict) else {}
    if _resolve_sell_loop_strategy_type(p) == "SWING_FIB":
        return "스윙"
    if str(p.get("strategy_type") or "").strip().upper() == "TREND_V8":
        return "V8"
    th = _strategy_from_trade_history_buy(ticker, market)
    if th:
        return th
    return "V8"


def _heartbeat_fetch_ohlcv_for_holding(market: str, ticker: str) -> list:
    """생존신고·GUI — ``get_cached_ohlcv``(KIS·yfinance 교차검증, 매도 루프와 동일)."""
    m = str(market or "").strip().upper()
    t = str(ticker or "").strip()
    if not t:
        return []
    try:
        from utils.ohlcv_store import invalidate_disk_ohlcv, ohlcv_series_valid

        if m == "COIN":
            return coin_broker.fetch_ohlcv(t, "day", 250) or []
        if m == "KR":
            ohlcv = get_cached_ohlcv(t, broker=kis_api.broker_kr) or []
        elif m == "US":
            ohlcv = get_cached_ohlcv(t) or []
        else:
            return []
        if ohlcv and ohlcv_series_valid(ohlcv):
            return ohlcv
        invalidate_disk_ohlcv(t)
        _ohlcv_cache.pop(t, None)
        if m == "KR":
            return get_cached_ohlcv(t, broker=kis_api.broker_kr, force_refresh=True) or []
        if m == "US":
            return get_cached_ohlcv(t, force_refresh=True) or []
    except Exception:
        return []
    return []


def _heartbeat_resolve_sl_p(
    market: str, ticker: str, pos: dict, buy_p: float, curr_p: float
) -> float:
    """표시용 매도선 — 스윙/V8 각각 ``get_swing_exit_display_price`` / ``get_final_exit_price``."""
    p = pos if isinstance(pos, dict) else {}
    st = str(p.get("strategy_type") or "").strip().upper()
    tier = str(p.get("tier") or "").strip().upper()
    is_swing = st == "SWING_FIB" or tier in ("SWING_FIB", "SWING")
    cp = float(_to_float(curr_p, 0.0))
    bp = float(_to_float(buy_p, 0.0))
    ohlcv = _heartbeat_fetch_ohlcv_for_holding(market, ticker)
    if is_swing and ohlcv and len(ohlcv) >= 60 and cp > 0:
        pos2 = dict(p)
        pos2["max_p"] = max(float(_to_float(pos2.get("max_p", bp), bp)), cp)
        reconcile_swing_position(pos2, ohlcv, reference_price=cp)
        _, _, trading_h, _ = _compute_holding_time_info(pos2, market)
        sl = float(
            _resolve_exit_display_price(
                ticker,
                cp,
                pos2,
                ohlcv,
                "SWING_FIB",
                trading_hours_held=trading_h,
            )
        )
        if sl > 0:
            return sl
    if (not is_swing) and ohlcv and len(ohlcv) >= 20 and cp > 0:
        try:
            sl = float(get_final_exit_price(ticker, cp, p, ohlcv))
            if sl > 0:
                return sl
        except Exception:
            pass
    sl_fb = float(_to_float(p.get("sl_p", 0), 0.0))
    if sl_fb > 0:
        return sl_fb
    return bp * 0.9 if bp > 0 else 0.0


def _fmt_price_for_heartbeat(market: str, price: float) -> str:
    p = float(_to_float(price, 0.0))
    if market == "US":
        return f"${p:,.2f}"
    if market == "COIN":
        try:
            if coin_config.is_binance():
                return _fmt_telegram_coin_unit_usdt(p)
        except Exception:
            pass
        if 0 < p < 100:
            return f"{p:,.4f}원"
        return f"{int(p):,}원"
    return f"{int(p):,}원"


def _fmt_price_with_pct_vs_buy(market: str, price: float, buy_p: float) -> str:
    """가격 + 매수가 대비 %(텔레·GUI 공통)."""
    px = float(_to_float(price, 0.0))
    bp = float(_to_float(buy_p, 0.0))
    base = _fmt_price_for_heartbeat(market, px)
    if bp <= 0 or px <= 0:
        return base
    pct = (px / bp - 1.0) * 100.0
    return f"{base}({pct:+.2f}%)"


def build_holding_display_bundle(
    market: str,
    ticker: str,
    name: str,
    buy_p: float,
    curr_p: float,
    pos: dict,
    *,
    roi_pct: float | None = None,
    source_tag: str = "",
    line_prefix: str = "  ",
) -> dict:
    """
    보유 종목 표시 번들 — 텔레그램 생존신고·GUI 로그·표가 동일 포맷을 씁니다.

    Returns:
        line, strategy, buy_txt, curr_txt, max_txt, sl_txt, roi_pct, duration_clause
    """
    p = pos if isinstance(pos, dict) else {}
    strat = _heartbeat_strategy_label(p, ticker=ticker, market=market)
    buy_ref = float(_to_float(buy_p, 0.0))
    curr_ref = float(_to_float(curr_p, 0.0))
    roi = (
        float(roi_pct)
        if roi_pct is not None
        else (((curr_ref - buy_ref) / buy_ref) * 100.0 if buy_ref > 0 else 0.0)
    )
    max_p = float(_to_float(p.get("max_p", 0), 0.0))
    if max_p <= 0:
        max_p = curr_ref if curr_ref > 0 else buy_ref
    if curr_ref > 0:
        max_p = max(max_p, curr_ref)
    sl_p = _heartbeat_resolve_sl_p(market, ticker, p, buy_ref, curr_ref)
    buy_txt = _fmt_price_for_heartbeat(market, buy_ref) if buy_ref > 0 else "-"
    curr_txt = _fmt_price_with_pct_vs_buy(market, curr_ref, buy_ref)
    if sl_p > 0:
        sl_txt = _fmt_price_with_pct_vs_buy(market, sl_p, buy_ref)
    else:
        sl_txt = "-"
    if max_p > 0:
        max_txt = _fmt_price_with_pct_vs_buy(market, max_p, buy_ref)
    else:
        max_txt = "-"
    dur_txt = _holding_duration_clause(p, market)
    tag_txt = f" {source_tag}" if source_tag else ""
    disp_name = str(name or ticker).strip() or str(ticker)
    line = (
        f"{line_prefix}{ticker}({disp_name}) | 전략:{strat} | "
        f"매수가 {buy_txt} | "
        f"현재가 {curr_txt} | "
        f"최고가 {max_txt} | "
        f"매도선 {sl_txt}{dur_txt}{tag_txt}"
    )
    return {
        "line": line,
        "strategy": strat,
        "buy_txt": buy_txt,
        "curr_txt": curr_txt,
        "max_txt": max_txt,
        "sl_txt": sl_txt,
        "roi_pct": float(roi),
        "duration_clause": dur_txt,
    }


def format_holding_display_line(
    market: str,
    ticker: str,
    name: str,
    buy_p: float,
    curr_p: float,
    roi: float,
    pos: dict,
    *,
    source_tag: str = "",
    line_prefix: str = "  ",
) -> str:
    """텔레그램·GUI 공용 보유 한 줄 문자열."""
    return build_holding_display_bundle(
        market,
        ticker,
        name,
        buy_p,
        curr_p,
        pos,
        roi_pct=roi,
        source_tag=source_tag,
        line_prefix=line_prefix,
    )["line"]


def _format_holding_line(
    market: str,
    ticker: str,
    name: str,
    buy_p: float,
    curr_p: float,
    roi: float,
    pos: dict,
    *,
    source_tag: str = "",
) -> str:
    """하위 호환 — ``format_holding_display_line`` 와 동일."""
    return format_holding_display_line(
        market, ticker, name, buy_p, curr_p, roi, pos, source_tag=source_tag
    )


def resolve_display_current_price(market: str, ticker: str, buy_p: float, current_p_api=None) -> float:
    return _resolve_display_current_price(
        market,
        ticker,
        buy_p,
        current_p_api,
        to_float=_to_float,
        get_ohlcv_yfinance=get_ohlcv_yfinance,
    )


def normalize_us_current_p_api_for_display(
    buy_p: float,
    current_p_api,
    *,
    is_market_open_now: bool | None = None,
    is_weekend: bool | None = None,
):
    """
    US 표시 현재가 전처리(텔레그램/GUI 공용).
    - 유효한 API 현재가가 없으면 None
    - 비장중/주말·점검 창에서 장부 폴백값(current_p==avg_p)은 None으로 내려 yfinance 경로를 강제
    """
    return normalize_equity_current_p_api_for_display(
        market="US",
        buy_p=buy_p,
        current_p_api=current_p_api,
        is_market_open_now=is_market_open_now,
        is_weekend=is_weekend,
    )


def normalize_equity_current_p_api_for_display(
    market: str,
    buy_p: float,
    current_p_api,
    *,
    is_market_open_now: bool | None = None,
    is_weekend: bool | None = None,
):
    """
    KR/US 표시 현재가 전처리(텔레그램/GUI 공용).
    - 유효한 API 현재가가 없으면 None
    - KR/US 비장중(프리·애프터·점검·주말)에서 장부 폴백값(current_p==avg_p)은
      None으로 내려 외부 시세(yfinance) 경로를 강제
    """
    m = str(market or "").strip().upper()
    cp = float(_to_float(current_p_api, 0.0))
    bp = float(_to_float(buy_p, 0.0))
    if cp <= 0:
        return None
    if m not in ("KR", "US"):
        return cp
    if is_weekend is None:
        is_weekend = bool(kis_equities_weekend_suppress_window_kst())
    if is_market_open_now is None:
        is_market_open_now = bool(is_market_open(m))
    # 숫자 직렬화/반올림 차이를 고려한 허용오차
    if (is_weekend or (not is_market_open_now)) and bp > 0:
        # 숫자 직렬화/반올림 차이를 고려한 허용오차
        tol = max(0.01, abs(bp) * 1e-4)
        if abs(cp - bp) <= tol:
            return None
    return cp


def resolve_holding_display_price(
    market: str,
    ticker: str,
    buy_p: float,
    current_p_api,
    pos,
) -> float:
    """KR/US/COIN 보유 한 줄 표시용 현재가 — GUI·텔레그램·생존신고 동일."""
    m = str(market or "").strip().upper()
    pos = pos if isinstance(pos, dict) else {}
    bp = float(_to_float(buy_p, 0.0))
    t = str(ticker).strip()

    if m == "COIN":
        live = float(resolve_display_current_price("COIN", t, bp, current_p_api))
        return float(_resolve_curr_price_with_gui_override(pos, live))

    if m not in ("KR", "US"):
        return float(resolve_display_current_price(m, t, bp, current_p_api))

    if kis_equities_weekend_suppress_window_kst():
        return float(_resolve_curr_price_with_gui_override(pos, bp))
    is_open = bool(is_market_open(m))
    if m == "KR":
        cp_n = normalize_equity_current_p_api_for_display(
            market="KR",
            buy_p=bp,
            current_p_api=current_p_api,
            is_market_open_now=is_open,
            is_weekend=False,
        )
    else:
        cp_n = normalize_us_current_p_api_for_display(
            bp,
            current_p_api,
            is_market_open_now=is_open,
            is_weekend=False,
        )
    live = float(resolve_display_current_price(m, t, bp, cp_n))
    # 평일 장외: 장부에 저장된 마지막 현재가(curr_p)가 있으면 그걸 씀(코인과 동일). 장중만 라이브 그대로.
    if not is_open:
        return float(_resolve_curr_price_with_gui_override(pos, live))
    return live


resolve_equity_holding_display_price = resolve_holding_display_price


def _coin_snapshot_get_balance(quote: str = "KRW"):
    """스냅샷용 예수: 바이낸스는 USDT 가용을 원화 환산해 표시."""
    try:
        if coin_config.is_binance():
            b = coin_broker.get_quote_balance_direct()
            return int(float(b or 0) * float(coin_broker.get_krw_per_usdt()))
        if upbit_api.upbit is None:
            return 0
        return upbit_api.upbit.get_balance(quote)
    except Exception:
        return 0


def build_account_snapshot_for_report(
    *,
    allow_kis_fetch=None,
    with_backoff=None,
    force_kis_labels: bool = False,
    fresh_balances: bool = False,
    ledger_only: bool = False,
    kis_label_anomaly_prompt=None,
) -> dict:
    """GUI·heartbeat 스냅샷 — ``services.account_display`` 위임."""
    from services.account_display import build_account_snapshot_for_report as _impl

    return _impl(
        allow_kis_fetch=allow_kis_fetch,
        with_backoff=with_backoff,
        force_kis_labels=force_kis_labels,
        fresh_balances=fresh_balances,
        ledger_only=ledger_only,
        kis_label_anomaly_prompt=kis_label_anomaly_prompt,
    )


def _telegram_sl_clause(market: str, curr_p: float, pos: dict) -> str:
    """생존신고·보유 한 줄에 붙이는 매도선(sl_p) vs 현재가 여유(%p). 없으면 빈 문자열."""
    if not isinstance(pos, dict):
        return ""
    sl = _to_float(pos.get("sl_p", 0), 0.0)
    if sl <= 0 or curr_p <= 0:
        return ""
    try:
        pct = (float(curr_p) / float(sl) - 1.0) * 100.0
    except Exception:
        return ""
    if market == "KR":
        return f" · 매도선 {int(sl):,}원 (vs {pct:+.1f}%p)"
    if market == "US":
        return f" · 매도선 ${sl:.2f} (vs {pct:+.1f}%p)"
    if market == "COIN":
        try:
            if coin_config.is_binance():
                return f" · 매도선 {_fmt_telegram_coin_unit_usdt(float(sl))} (vs {pct:+.1f}%p)"
        except Exception:
            pass
    if float(sl) < 100:
        return f" · 매도선 {sl:,.4f}원 (vs {pct:+.1f}%p)"
    return f" · 매도선 {int(sl):,}원 (vs {pct:+.1f}%p)"


def _equity_ledger_source_tag(
    market: str,
    pos: dict,
    buy_p: float,
    curr_p: float,
    *,
    weekend_tag: bool,
) -> str:
    """텔레·스냅샷 장부 폴백 — KR/US 동일 (주말|장외)·(마지막현재가|장부평단) 태그."""
    has_last = float(pos.get("curr_p") or 0) > 0 and abs(float(curr_p) - float(buy_p)) > 1e-9
    if weekend_tag:
        return "(주말·마지막현재가)" if has_last else "(주말·장부평단)"
    if not bool(is_market_open(market)):
        return "(장외·마지막현재가)" if has_last else "(장외·장부평단)"
    return ""


def _kr_holdings_lines_from_ledger(state: dict, *, weekend_tag: bool) -> list:
    """KIS 점검·장 개시 전 등 — ``gui_table_adapter`` 장부 폴백과 동일 소스로 보유 줄 생성."""
    holdings = []
    for code, pos in (state.get("positions") or {}).items():
        if not str(code).isdigit():
            continue
        buy_p = _to_float(pos.get("buy_p", 0), 0)
        if buy_p <= 0:
            continue
        curr_p = _resolve_curr_price_with_gui_override(pos, float(buy_p))
        roi = ((curr_p - buy_p) / buy_p) * 100 if buy_p > 0 else 0.0
        kr_name = get_kr_company_name(code)
        tag = _equity_ledger_source_tag(
            "KR", pos, float(buy_p), float(curr_p), weekend_tag=weekend_tag
        )
        holdings.append(
            _format_holding_line(
                "KR",
                code,
                kr_name,
                float(buy_p),
                float(curr_p),
                float(roi),
                pos,
                source_tag=tag,
            )
        )
    return holdings


def _us_holdings_lines_from_ledger(state: dict) -> list:
    """미장 KIS 상세가 비었을 때(장 외 등) — GUI ``get_held_stocks_us_info`` 폴백과 동일 소스."""
    is_weekend = bool(kis_equities_weekend_suppress_window_kst())
    holdings = []
    for ticker_raw, pos_u in (state.get("positions") or {}).items():
        t = normalize_ticker(str(ticker_raw))
        if not t or str(t).isdigit() or is_coin_ticker(t):
            continue
        if not isinstance(pos_u, dict):
            pos_u = {}
        buy_p = _to_float(pos_u.get("buy_p", 0), 0.0)
        if buy_p <= 0:
            continue
        curr_p = _resolve_curr_price_with_gui_override(pos_u, float(buy_p))
        roi = ((curr_p - buy_p) / buy_p) * 100
        us_name = get_us_company_name(t)
        tag = _equity_ledger_source_tag(
            "US", pos_u, float(buy_p), float(curr_p), weekend_tag=is_weekend
        )
        holdings.append(
            _format_holding_line(
                "US",
                t,
                us_name,
                float(buy_p),
                float(curr_p),
                float(roi),
                pos_u,
                source_tag=tag,
            )
        )
    return holdings


def get_kr_holdings_with_roi():
    """🇰🇷 국장 보유 종목 + 현재 수익률 (balance API 현재가 사용)"""
    try:
        state = load_state(STATE_PATH)
        from execution.balance_policy import should_use_ledger_only

        if kis_equities_weekend_suppress_window_kst():
            return _kr_holdings_lines_from_ledger(
                state, weekend_tag=bool(kis_equities_weekend_suppress_window_kst())
            )
        if should_use_ledger_only(state, config, force=False) and not bool(
            is_market_open("KR")
        ):
            return _kr_holdings_lines_from_ledger(state, weekend_tag=False)
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
            buy_p = _to_float(pos.get("buy_p", 0), 0)
            api_avg = _to_float(
                stock.get("pchs_avg_prc", stock.get("pchs_avg_pric", 0)),
                0.0,
            )
            if buy_p <= 0 and api_avg > 0:
                buy_p = float(api_avg)
            if buy_p <= 0:
                continue
            
            curr_p = resolve_holding_display_price(
                "KR",
                code,
                buy_p,
                stock.get("prpr"),
                pos,
            )
                
            roi = ((curr_p - buy_p) / buy_p) * 100
            kr_name = get_kr_company_name(code)
            holdings.append(
                _format_holding_line(
                    "KR",
                    code,
                    kr_name,
                    float(buy_p),
                    float(curr_p),
                    float(roi),
                    pos,
                )
            )
        # 장 개시 전: KIS output1이 비었거나(또는 장부 평단 미기입으로 전부 스킵) GUI는 장부 폴백을 쓴다 → 텔레도 동일
        if not holdings and not bool(is_market_open("KR")):
            return _kr_holdings_lines_from_ledger(state, weekend_tag=False)

        return holdings
    except Exception:
        return []

def get_us_holdings_with_roi():
    """🇺🇸 미장 보유 종목 + 현재 수익률"""
    try:
        state = load_state(STATE_PATH)
        from execution.balance_policy import should_use_ledger_only

        if kis_equities_weekend_suppress_window_kst():
            return _us_holdings_lines_from_ledger(state)
        if should_use_ledger_only(state, config, force=False) and not bool(
            is_market_open("US")
        ):
            return _us_holdings_lines_from_ledger(state)
        # GUI와 동일한 함수 사용
        us_data = get_held_stocks_us_detail()
        if not us_data:
            if not bool(is_market_open("US")):
                return _us_holdings_lines_from_ledger(state)
            return []

        holdings = []
        for item in us_data:
            ticker = normalize_ticker(item['code'])
            qty = item['qty']
            buy_p = _to_float(item.get('avg_p', 0), 0.0)
            
            if buy_p <= 0:
                continue
            
            pos_u = state.get("positions", {}).get(ticker, {})
            if not isinstance(pos_u, dict):
                pos_u = {}
            curr_p = resolve_holding_display_price(
                "US",
                ticker,
                buy_p,
                item.get("current_p", 0),
                pos_u,
            )
            
            roi = ((curr_p - buy_p) / buy_p) * 100
            us_name = get_us_company_name(ticker)
            holdings.append(
                _format_holding_line(
                    "US",
                    ticker,
                    us_name,
                    float(buy_p),
                    float(curr_p),
                    float(roi),
                    pos_u if isinstance(pos_u, dict) else {},
                )
            )
        # 국장과 동일: 비장중·평단 미기입으로 한 줄도 못 만들었으면 장부 폴백 (텔레·스냅샷과 일치)
        if not holdings and not bool(is_market_open("US")):
            return _us_holdings_lines_from_ledger(state)

        return holdings
    except Exception as e:
        print(f"⚠️ US 보유종목 조회 에러: {e}")
        return []

def get_coin_holdings_with_roi():
    """🪙 코인 보유 종목 + 현재 수익률"""
    try:
        state = load_state(STATE_PATH)
        balances = coin_broker.get_balances() or []
        
        holdings = []
        for b in balances:
            if b['currency'] in ('KRW', 'VTHO'):
                continue
            if coin_config.is_binance() and str(b.get("currency", "")).upper() == "USDT":
                continue
            qty = _to_float(b.get('balance', 0))
            if not coin_broker.should_include_coin_balance_row(b):
                continue

            ticker = coin_broker.held_ticker_row(b)
            if not ticker:
                continue
            pos = state.get('positions', {}).get(ticker, {})
            buy_p = _to_float(pos.get('buy_p', 0), 0)

            # 매수가가 없으면 avg_buy_price 사용
            if buy_p <= 0:
                buy_p = _to_float(b.get('avg_buy_price', 0), 0)
            if buy_p <= 0:
                buy_p = float(_last_buy_price_from_trade_history(ticker, "COIN") or 0.0)

            if buy_p <= 0:
                continue

            curr_p = resolve_holding_display_price("COIN", ticker, buy_p, None, pos)

            roi = ((curr_p - buy_p) / buy_p) * 100
            coin_name = get_coin_name(ticker)
            holdings.append(
                _format_holding_line(
                    "COIN",
                    ticker,
                    coin_name,
                    float(buy_p),
                    float(curr_p),
                    float(roi),
                    pos,
                )
            )

        return holdings
    except Exception as e:
        print(f"⚠️ 코인 보유종목 조회 에러: {e}")
        return []

def heartbeat_report():
    """텔레그램 생존신고 — ``services.heartbeat_report`` 위임."""
    from services.heartbeat_report import run_heartbeat_report

    run_heartbeat_report()


# =====================================================================
# 6. 메인 매매 엔진 — ``run_trading_bot()`` 한 번이 곧 한 사이클(매도→매수 파이프라인)
# ---------------------------------------------------------------------
# 이 블록은 **주문·조회·동기화**가 한 사이클에 모이므로, 디버깅 시 다음 순서로 로그를 추적하면 된다.
#   1) ``_prepare_cycle_state`` — 장부 로드·키 정규화·KIS/업비트 토큰 갱신
#   2) ``_sync_positions_for_cycle`` — 코인 실조회 + 국·미는 장중에만 KIS 보유 조회 후 ``sync_all_positions``
#      (실패 시 ``[장부 동기화 건너뜀]`` + 실패 시장 목록, ``sync_positions`` 모듈이 이어서 상세 출력)
#   3) ``_build_market_context`` — 날씨·거시(macro_mult)·합산 서킷
#   4) 시장별 엔진 — 매도 루프(방어) 후 매수 루프(진입). 매수는 **시간창·지수·날씨·예산·시그널·AI·TWAP** 순으로 게이트.
# =====================================================================
def _prepare_cycle_state() -> dict:
    """
    트레이딩 사이클 시작 전 **장부 로드 + 키 정규화 + 브로커 토큰 준비**.

    반환값은 항상 ``load_state`` 결과이며, 여기서는 주문을 넣지 않는다.
    ``normalize_positions_keys`` 가 True면 장부가 수정된 것이므로 즉시 저장하고 로그를 남긴다.
    """
    state = load_state(STATE_PATH)
    if normalize_positions_keys(state):
        save_state(STATE_PATH, state)
        print("  🔧 [장부 정규화] positions 키 포맷 정리 완료")
    order_idem.ensure_idempotency_state(state)
    pruned = order_idem.prune_order_idempotency(state)
    if pruned > 0:
        save_state(STATE_PATH, state)
    from execution.balance_policy import wants_live_kis_balance

    if wants_live_kis_balance(state, config, force=False):
        bal_read.invalidate()
    refresh_brokers_if_needed()
    return state


def _sync_positions_for_cycle(state: dict) -> None:
    """
    실계좌 보유와 ``bot_state.positions`` 를 맞춘다.

    - 코인은 항상 실조회. 국·미는 **정규장**일 때만 KIS 보유 목록을 조회하고, 비장중에는
      ``[]`` 로 넘겨 API를 부르지 않는다.
    - 세 조회가 **모두 성공**해야 ``sync_all_positions`` 를 호출한다(국·미 비장중 ``[]`` 는 성공).
    - 하나라도 ``None`` 이면 동기화를 **건너뛰고** 기존 장부를 유지한다(부분 정보로 유령 삭제하는 것을 방지).
      이 경우 반드시 ``[장부 동기화 건너뜀]`` 로그가 출력된다.
    """
    held_kr, held_us = fetch_equity_held_lists_for_position_sync()
    held_coins = get_held_coins()

    if held_kr is not None and held_us is not None and held_coins is not None:
        sync_all_positions(state, held_kr, held_us, held_coins, STATE_PATH)
        return

    failed_apis = []
    if held_kr is None:
        failed_apis.append("국장")
    if held_us is None:
        failed_apis.append("미장")
    if held_coins is None:
        failed_apis.append("코인")
    error_msg = f"실보유 조회 실패 ({', '.join(failed_apis)} API 오류)"
    print(f"  ⚠️ [장부 동기화 건너뜀] {error_msg} - 기존 장부 유지")


def _macro_market_buy_allowed(macro_snap: dict, market: str) -> bool:
    allowed = (macro_snap or {}).get("market_buy_allowed") or {}
    return bool(allowed.get(str(market or "").strip().upper(), True))


def _hedge_tickers_for_market(market: str) -> list[str]:
    """``strategy.hedge_universe`` 위임 — COIN 은 거래소 접두 티커."""
    mk = str(market or "").strip().upper()
    if mk == "COIN":
        from strategy.hedge_universe import coin_hedge_internal_tickers

        return coin_hedge_internal_tickers(is_binance=coin_config.is_binance())
    return hedge_tickers_for_market(market)


def _is_hedge_ticker(ticker: str, market: str) -> bool:
    t = normalize_ticker(ticker)
    mk = str(market or "").strip().upper()
    if mk == "COIN":
        return is_coin_hedge_internal_ticker(t)
    hedge_set = {normalize_ticker(h) for h in _hedge_tickers_for_market(market)}
    return bool(t) and t in hedge_set


def _merge_hedge_into_buy_targets(buy_targets: list[str], market: str) -> list[str]:
    """매수 후보에 헷지 티커를 무조건 포함(중복 제거, 기존 순서 유지)."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in list(buy_targets or []):
        t = normalize_ticker(raw)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    for h in _hedge_tickers_for_market(market):
        ht = normalize_ticker(h)
        if ht and ht not in seen:
            seen.add(ht)
            out.append(ht)
    return out


def _apply_phase4_hedge_buy_targets(
    buy_targets: list[str], macro_snap: dict, market: str
) -> list[str]:
    """Phase4(``market_buy_allowed`` false) 시 일반 종목 제거, 헷지만 남김."""
    if _macro_market_buy_allowed(macro_snap, market):
        return list(buy_targets or [])
    hedge_set = {normalize_ticker(h) for h in _hedge_tickers_for_market(market)}
    filtered = [
        t for t in (buy_targets or []) if normalize_ticker(t) in hedge_set
    ]
    if filtered:
        mk_u = str(market or "").strip().upper()
        if mk_u == "COIN":
            print(
                "  🚨 [Phase 4 발동] 일반 코인 매수 차단 -> "
                "하락장 헷지 자산(금 토큰)만 매수 검토"
            )
        else:
            print(
                "  🚨 [Phase 4 발동] 주식 매수 차단 -> 하락장 헷지 자산만 매수 검토"
            )
        print(
            f"  🛡️ [헷지 유니버스 {mk_u}] "
            f"{format_hedge_universe_summary(market)} "
            f"(수정: strategy/hedge_universe.py)"
        )
    return filtered


def _can_open_new_respecting_hedge_bypass(
    ticker: str, state: dict, market: str, max_positions: int
) -> bool:
    """헷지 티커는 ``MAX_POSITIONS`` 슬롯 검사를 우회(예수금·portfolio heat는 별도)."""
    if _is_hedge_ticker(ticker, market):
        return True
    return can_open_new(ticker, state, max_positions=max_positions)


def _phase4_hedge_only_active(macro_snap: dict, market: str) -> bool:
    """Phase4로 일반 주식 매수가 막힌 상태(헷지 전용 모드)."""
    return not _macro_market_buy_allowed(macro_snap, market)


def _refresh_kis_display_snapshot_after_trade(state: dict, market: str) -> None:
    """수동·자동 매매 직후 KIS 예수·총평 스냅샷을 실조회로 갱신."""
    mk = str(market or "").strip().upper()
    try:
        if mk == "KR":
            kc, te = _refresh_kr_cash_equity_after_sells()
            _sync_market_display_snapshot_after_sells("KR", state, kc, te)
        elif mk == "US":
            uc, te = _refresh_us_cash_equity_after_sells()
            _sync_market_display_snapshot_after_sells("US", state, uc, te)
        elif mk == "COIN":
            bal_read.invalidate("COIN")
            balances = coin_broker.get_balances() or []
            krw_on, krw_bal = _compute_coin_krw_balances(balances)
            total = int(_compute_total_coin_equity_from_balances(balances, float(krw_on)))
            coin_m = _calc_coin_holdings_metrics(balances, state.get("positions"))
            save_last_coin_display_snapshot(int(krw_bal), total, coin_m.get("roi"))
            state["circuit_aux_last_coin_krw"] = float(total)
            save_state(STATE_PATH, state)
    except Exception as e:
        print(f"  ⚠️ [{mk}] 매매 후 표시 스냅샷 갱신 실패: {type(e).__name__}: {e}")


def _sync_market_display_snapshot_after_sells(
    market: str,
    state: dict,
    cash: float,
    total_equity: float,
) -> None:
    """매도 직후 KIS 실조회값을 ``last_kis_display_snapshot``·GUI·텔레에 반영."""
    from services import ledger_valuation as lv

    mk = str(market or "").strip().upper()
    roi = None
    try:
        if mk == "KR":
            bal = ensure_dict(bal_read.kr_balance_raw(refresh=False))
            roi = _calc_kr_holdings_metrics(bal).get("roi")
        elif mk == "US":
            bal = ensure_dict(bal_read.us_balance_raw(refresh=False))
            roi = _calc_us_holdings_metrics(bal).get("roi")
    except Exception:
        pass
    lv.write_kis_display_snapshot_part(
        state,
        mk,
        cash=float(cash),
        total=float(total_equity),
        roi=roi,
        force=True,
    )
    save_state(STATE_PATH, state)
    if mk == "KR":
        print(
            f"  📌 [KR] KIS 예수·총평 스냅샷 갱신 → 가용 {int(cash):,}원 · "
            f"총평 {int(total_equity):,}원 (매도 후 GUI·텔레 반영)"
        )
    else:
        print(
            f"  📌 [US] KIS 예수·총평 스냅샷 갱신 → 가용 ${float(cash):,.2f} · "
            f"총평 ${float(total_equity):,.2f} (매도 후 GUI·텔레 반영)"
        )


def _benchmark_ticker_for_rs(market: str) -> str:
    mk = str(market or "").strip().upper()
    if mk == "KR":
        return "^KS11"
    if mk == "COIN":
        return coin_config.btc_benchmark_ticker()
    return "^GSPC"


def _sort_buy_targets_by_rs(tickers: list[str], market: str) -> list[str]:
    if not tickers:
        return []
    try:
        bench = _benchmark_ticker_for_rs(market)
        mk = str(market or "").strip().upper()

        def _fetch(ticker: str) -> list:
            if mk == "COIN":
                return coin_broker.fetch_ohlcv(ticker, "day", 120) or []
            return get_ohlcv_yfinance(ticker) or []

        ordered = sort_targets_by_relative_strength(
            list(tickers),
            market,
            fetch_ohlcv=_fetch,
            fetch_benchmark_ohlcv=_fetch,
            benchmark_ticker=bench,
        )
        print(f"  -> [RS] {market} 후보 {len(ordered)}개 10일 상대강도 순 정렬 (벤치={bench})")
        return ordered
    except Exception as e:
        print(f"  ⚠️ [RS] {market} 정렬 실패 — 원본 순서 유지: {type(e).__name__}: {e}")
        return list(tickers)


def _position_ratio_with_vol_target(
    base_ratio: float,
    ohlcv: list,
    *,
    target_vol: float,
    ticker: str = "",
) -> tuple[float, str]:
    br = float(base_ratio)
    if not ohlcv:
        return br, "1/N 고정"
    try:
        atr_val = float(get_safe_atr(ticker, ohlcv) or 0.0)
        close_px = float(ohlcv[-1].get("c", 0) or 0.0)
        ratio = volatility_target_ratio(br, atr_val, close_px, target_vol=float(target_vol))
        if ratio + 1e-12 < br:
            return float(ratio), f"vol-target(ATR%, cap 1/N={br:.4f})"
    except Exception:
        pass
    return br, "1/N 고정"


def _build_market_context(state: dict) -> tuple[dict, float, str, dict]:
    """시장 날씨/거시 컨텍스트 계산 + 계좌 서킷 점검."""
    weather = get_real_weather(kis_api.broker_kr, kis_api.broker_us)
    print(f"🌡️ 시장 날씨: 국장 {weather['KR']} / 미장 {weather['US']} / 코인 {weather['COIN']}")

    _macro_snap = get_macro_guard_snapshot(config)
    macro_mult = float(_macro_snap.get("budget_multiplier", 1.0))
    macro_reason = str(_macro_snap.get("reason", "") or "")
    if _macro_snap.get("enabled"):
        print(f"  🛡️ [Phase4 거시] {_macro_snap.get('mode')} | {macro_reason}")
        pcr = _macro_snap.get("us_put_call_ratio")
        whale = _macro_snap.get("coin_whale_long_short_ratio")
        fx_mom = _macro_snap.get("usd_krw_momentum_ratio")
        if pcr is not None or whale is not None or fx_mom is not None:
            print(
                f"  🛡️ [Phase4 글로벌] PCR={pcr if pcr is not None else 'n/a'} "
                f"고래롱숏={whale if whale is not None else 'n/a'} "
                f"환율모멘텀={fx_mom if fx_mom is not None else 'n/a'}"
            )
        for mk in ("KR", "US", "COIN"):
            if not _macro_market_buy_allowed(_macro_snap, mk):
                print(
                    f"  🚫 [Phase4 글로벌] {mk} 신규 매수 차단 — "
                    f"{(_macro_snap.get('market_buy_block_reason') or {}).get(mk, '')}"
                )
    else:
        print(f"  🛡️ [Phase4 거시] 비활성 | {macro_reason}")

    from execution.balance_policy import should_use_ledger_only, clear_balance_live_sync
    from services import ledger_valuation as lv

    try:
        if should_use_ledger_only(state, config, force=False):
            coin_eq = float(state.get("circuit_aux_last_coin_krw", 0) or 0)
            try:
                balances = coin_broker.get_balances() or []
                if balances:
                    _, coin_eq = _compute_total_coin_equity_from_balances(
                        balances, float(_compute_coin_krw_balances(balances)[0])
                    )
            except Exception:
                pass

            def _kr_p(c, p, b):
                return float(resolve_holding_display_price("KR", c, b, None, p))

            def _us_p(t, p, b):
                return float(resolve_holding_display_price("US", t, b, None, p))

            aux_info = lv.update_circuit_aux_from_ledger(
                state,
                resolve_kr_price=_kr_p,
                resolve_us_price=_us_p,
                estimate_usdkrw=estimate_usdkrw,
                coin_equity_krw=coin_eq,
            )
            save_state(STATE_PATH, state)
            print(
                "  [Phase5 보조] 장부+시세 추정 — KIS 잔고 API 생략 "
                f"(합계 약 {aux_info.get('totals', {}).get('total_krw_est', 0):,.0f}원)"
            )
        else:
            aux_info = refresh_circuit_aux_from_brokers(state, STATE_PATH)
            clear_balance_live_sync(state, STATE_PATH)
        if isinstance(aux_info, dict):
            totals = aux_info.get("totals") if isinstance(aux_info.get("totals"), dict) else {}
            ledger_only = bool(aux_info.get("ledger_only"))
            sync: dict = {
                "kr_ok": bool(aux_info.get("kr_ok")),
                "us_ok": bool(aux_info.get("us_ok")),
                "coin_ok": bool(aux_info.get("coin_ok")),
                "weekend_kis_skip": bool(aux_info.get("weekend_kis_skip")),
                "ledger_only": ledger_only,
            }
            if ledger_only and totals:
                sync["kr_krw"] = float(totals.get("kr_krw", 0) or 0)
                sync["usd_total"] = float(totals.get("usd_total", 0) or 0)
            state["_phase5_aux_sync"] = sync
    except Exception as e:
        state["_phase5_aux_sync"] = {"kr_ok": False, "us_ok": False, "coin_ok": False}
        print(f"  ⚠️ [Phase5 보조값] circuit_aux 갱신 실패 — 이번 루프 서킷 판정은 건너뜀: {type(e).__name__}: {e}")

    _maybe_run_account_circuit(state)
    return weather, macro_mult, macro_reason, _macro_snap


def _build_kr_targets(scanned_targets: list[str], market_cap_200: list[str], top_vol_50: list[str]) -> list[str]:
    """국장 최종 타깃 구성(기존 tier 분류 로직 분리)."""
    tier_1 = []
    tier_2 = []
    tier_3 = []
    for t in scanned_targets:
        is_large_cap = t in market_cap_200
        is_high_vol = t in top_vol_50
        if is_large_cap and is_high_vol:
            tier_1.append(t)
        elif is_large_cap and not is_high_vol:
            tier_2.append(t)
        elif not is_large_cap and is_high_vol:
            tier_3.append(t)
    final_targets = tier_1 + tier_2 + tier_3
    print(f"  -> 🌐 [국장 타겟] 1티어({len(tier_1)}개) 포함 총 {len(final_targets)}개")
    return final_targets


def _extract_held_kr_codes_from_output1(kr_output1: list[dict]) -> list[str]:
    """KR output1에서 실제 보유 종목 코드만 추출."""
    return extract_held_kr_codes(kr_output1, _to_float, normalize_ticker)


def _extract_held_us_codes_from_output1(us_output1: list[dict]) -> list[str]:
    """US output1에서 실제 보유 종목 코드만 추출."""
    return extract_held_us_codes(us_output1, _to_float, normalize_ticker)


def _compute_us_stock_value_from_output(us_bal: dict, out2) -> float:
    """US 주식 평가금 — ``api.kis_parsers`` 위임."""
    return compute_us_stock_value_from_output(us_bal, out2, _to_float)


def _recover_us_cash_from_output2_if_needed(us_cash: float, out2) -> float:
    """US 현금이 0일 때 output2 기반 fallback 복구(기존 로직 동일)."""
    if us_cash <= 0.0 and out2:
        try:
            # API가 0원이라고 뻥을 쳐도, 잔고표(output2)를 뒤져서 진짜 외화예수금(frcr_dncl_amt_2)을 찾아냄
            return float(parse_us_cash_fallback(out2, _to_float))
        except Exception as e:
            print(f"⚠️ 야간 예수금 복구 중 에러 발생: {e}")
    return float(us_cash)


def _collect_us_sell_candidates(held_us: list[str], positions: dict) -> list[str]:
    """US 매도 대상 포지션 목록 계산(기존 로직 동일)."""
    return [code for code in held_us if code in positions]


def _prefetch_us_sell_ohlcv_if_needed(sell_candidates: list[str]) -> None:
    """US 매도 대상 OHLCV 프리패치(기존 로직 동일)."""
    if not sell_candidates:
        print(f"  ✅ [미장 매도 루프] 매도할 종목 없음 (완료)")
        return
    prefetch_ohlcv(sell_candidates, market="US")


def _log_us_holdings_debug(held_us: list[str], us_bal: dict) -> None:
    """US 보유 인식 결과 디버그 로그(기존 출력 유지)."""
    print(f"  🔍 [US 잔고 데이터] 인식된 종목 수: {len(held_us)}개 / 리스트: {held_us}")
    if held_us:
        return
    bal = us_bal if isinstance(us_bal, dict) else {}
    rt = str(bal.get("rt_cd", "")).strip()
    if rt and rt != "0":
        print(f"  ⚠️ [US API 메시지] {bal.get('msg_cd', '')}: {bal.get('msg1', '')}")
        return
    # rt_cd=0 이고 보유 0건 — KIS 정상 응답(예: msg1「조회되었습니다」)은 경고 아님


def _get_us_output1(us_bal: dict) -> list[dict]:
    """US 잔고 응답에서 output1 리스트 추출(기존 로직 동일)."""
    return ensure_list(us_bal.get("output1", []))


def _get_kr_output1(kr_bal: dict) -> list[dict]:
    """KR 잔고 응답에서 output1 리스트 추출(기존 로직 동일)."""
    return kr_bal.get("output1", []) if isinstance(kr_bal.get("output1"), list) else []


def _get_us_output2(us_bal: dict):
    """US 잔고 응답에서 output2 추출(기존 로직 동일)."""
    return safe_get(us_bal, "output2", {})


def _count_positions_in_state(codes: list[str], positions: dict) -> int:
    """코드 목록 중 state positions에 존재하는 개수(기존 로직 동일)."""
    return len([code for code in codes if code in positions])


def _prepare_kr_market_cycle_inputs(state: dict) -> tuple[dict, int, int, list[dict], list[str]]:
    """KR 매매 루프 입력값 준비(기존 로직 동일)."""
    bal = ensure_dict(get_balance_with_retry())
    kr_balance_data = bal.get("output2", [])
    kr_cash, total_kr_equity = parse_kr_cash_total(kr_balance_data, _to_float)

    from services import ledger_valuation as lv

    lv.write_kis_display_snapshot_part(
        state, "KR", cash=float(kr_cash), total=float(total_kr_equity)
    )
    save_state(STATE_PATH, state)

    kr_output1 = _get_kr_output1(bal)
    held_kr = _extract_held_kr_codes_from_output1(kr_output1)
    return bal, kr_cash, total_kr_equity, kr_output1, held_kr


def _refresh_kr_cash_equity_after_sells() -> tuple[int, int]:
    """매도 루프 직후·매수 직전: **실 KIS 잔고**로 예수·총평가 갱신(``refresh=True``)."""
    _kis_post_trade_balance_pause()
    bal_read.invalidate("KR")
    bal = ensure_dict(bal_read.kr_balance_raw(refresh=True))
    kr_balance_data = bal.get("output2", [])
    kr_cash, total_kr_equity = parse_kr_cash_total(kr_balance_data, _to_float)
    return int(kr_cash), int(total_kr_equity)


def _refresh_us_cash_equity_after_sells() -> tuple[float, float]:
    """미장 매도 루프 직후·매수 직전: **실 KIS 잔고**로 예수·총자산 갱신."""
    _kis_post_trade_balance_pause()
    bal_read.invalidate("US")
    us_cash = float(get_us_cash_real(kis_api.broker_us, refresh=True) or 0.0)
    us_bal = ensure_dict(bal_read.us_balance_raw(refresh=True))
    out2 = _get_us_output2(us_bal)
    us_cash = _recover_us_cash_from_output2_if_needed(us_cash, out2)
    us_stock_value = _compute_us_stock_value_from_output(us_bal, out2)
    total_us_equity = float(us_cash + us_stock_value)
    return float(us_cash), total_us_equity


def _prefetch_kr_sell_ohlcv_if_needed(kr_output1: list[dict], held_kr: list[str], positions_count: int) -> None:
    """KR 매도 대상 OHLCV 프리패치 및 로그(기존 로직 동일)."""
    print(f"  🔍 [국장 매도 루프] 보유 포지션 {positions_count}개 손익 체크 시작...")
    if positions_count == 0:
        print(f"  ✅ [국장 매도 루프] 매도할 종목 없음 (완료)")
        return
    kr_sell_tickers = [
        normalize_ticker(s.get("pdno", ""))
        for s in kr_output1
        if normalize_ticker(s.get("pdno", "")) in held_kr
    ]
    prefetch_ohlcv(kr_sell_tickers, market="KR", broker=kis_api.broker_kr)


def _log_kr_market_closed_or_suppressed() -> None:
    """KR 비장중/주말점검 로그 출력(기존 분기 메시지 동일)."""
    if is_market_open("KR") and kis_equities_weekend_suppress_window_kst():
        print("💤 [주말 점검] 국장 매매 엔진 — 증권사 API 점검 구간으로 KIS 호출을 생략합니다.")
    else:
        print("💤 국장은 현재 휴장 상태입니다.")


def _log_us_market_closed_or_suppressed() -> None:
    """US 비장중/주말점검 로그 출력(기존 분기 메시지 동일)."""
    if is_market_open("US") and kis_equities_weekend_suppress_window_kst():
        print("💤 [주말 점검] 미장 매매 엔진 — 증권사 API 점검 구간으로 KIS 호출을 생략합니다.")
    else:
        print("💤 미장은 현재 휴장 상태입니다.")


def _is_kr_buy_window_now(now_kr: datetime) -> tuple[bool, datetime, datetime]:
    """KR 매수 시간창 계산(기존 로직 동일)."""
    kr_close = now_kr.replace(hour=15, minute=30, second=0, microsecond=0)
    kr_buy_start = kr_close - timedelta(minutes=BUY_WINDOW_MINUTES_BEFORE_CLOSE)
    return kr_buy_start <= now_kr < kr_close, kr_buy_start, kr_close


def _is_us_buy_window_now(now_us: datetime) -> tuple[bool, datetime, datetime]:
    """US 매수 시간창 계산(기존 로직 동일, ET 기준 16:00 마감)."""
    us_close = now_us.replace(hour=16, minute=0, second=0, microsecond=0)
    us_buy_start = us_close - timedelta(minutes=BUY_WINDOW_MINUTES_BEFORE_CLOSE)
    return us_buy_start <= now_us < us_close, us_buy_start, us_close


def _is_coin_buy_window_now(now_coin: datetime) -> tuple[bool, datetime, datetime]:
    """COIN 매수 시간창. ``coin_close`` = 매일 KST 09:00.

    * **바이낸스(CCXT) 1d** 캔들은 관례상 **UTC 00:00** 경계(새 일봉 시작). 한국은 UTC+9·서머타임 없음 →
      **KST 09:00 = UTC 00:00** 이므로, 이 ``coin_close`` 는 바이낸스 일봉이 갱신되는 순간과 같다.
    * **업비트** 일봉이 API/차트마다 **KST 자정(00:00)** 인 경우도 있어, 엄밀히는 거래소 일봉 정의와
      1~2h 오차가 날 수 있다(전략이 09:00 KST를 “국제가 일봉 기준”으로 쓰는 셈).
    """
    coin_close = now_coin.replace(hour=9, minute=0, second=0, microsecond=0)
    coin_buy_start = coin_close - timedelta(minutes=BUY_WINDOW_MINUTES_BEFORE_CLOSE)
    return coin_buy_start <= now_coin < coin_close, coin_buy_start, coin_close


def _extract_held_coins_from_balances(balances) -> list[str]:
    """거래소 balances에서 유효 보유 코인 티커 추출."""
    out: list[str] = []
    for b in balances:
        if b.get("currency") in ["KRW", "VTHO"]:
            continue
        if coin_config.is_binance() and str(b.get("currency", "")).upper() == "USDT":
            continue
        if not coin_broker.should_include_coin_balance_row(b):
            continue
        t = coin_broker.held_ticker_row(b)
        if not t:
            continue
        avg = float(_to_float(b.get("avg_buy_price", 0)))
        if avg > 0 or coin_config.is_binance():
            out.append(t)
    return out


def _compute_coin_krw_balances(balances) -> tuple[float, float]:
    """코인 예수 장부/주문가능 금액(바이낸스는 USDT를 원화 환산해 동일 변수명 유지)."""
    if coin_config.is_binance():
        kpx = float(coin_broker.get_krw_per_usdt())
        usdt_row = next((b for b in balances if str(b.get("currency", "")).upper() == "USDT"), None) or {}
        usdt_on_book = _to_float(usdt_row.get("balance", 0), 0.0)
        spend_usdt = coin_broker.quote_spendable(balances)
        krw_on_book = usdt_on_book * kpx
        krw_bal = spend_usdt * kpx
        if usdt_on_book > spend_usdt + 1e-6:
            print(
                f"  💡 [코인] USDT 장부 {usdt_on_book:.4f} 중 "
                f"{usdt_on_book - spend_usdt:.4f}는 locked — 주문가능 약 {spend_usdt:.4f} USDT "
                f"(원화환산 주문가능 약 {krw_bal:,.0f}원)"
            )
        return float(krw_on_book), float(krw_bal)
    krw_row = next((b for b in balances if str(b.get("currency", "")).upper() == "KRW"), None) or {}
    krw_on_book = _to_float(krw_row.get("balance", 0), 0.0)
    krw_bal = _upbit_krw_spendable(balances)
    if krw_on_book > krw_bal + 1.0:
        print(
            f"  💡 [코인] KRW 장부 {krw_on_book:,.0f}원 중 "
            f"{krw_on_book - krw_bal:,.0f}원은 locked(미체결·출금대기 등) — 주문가능 {krw_bal:,.0f}원"
        )
    return float(krw_on_book), float(krw_bal)


def _compute_total_coin_equity_from_balances(balances, krw_on_book: float) -> float:
    """코인 총평가금 계산(원화 기준; 바이낸스는 USDT×환율)."""
    total_coin_equity = float(krw_on_book)
    kpx = float(coin_broker.get_krw_per_usdt()) if coin_config.is_binance() else 1.0
    for b in balances:
        if b.get("currency") in ["KRW", "VTHO"]:
            continue
        if coin_config.is_binance() and str(b.get("currency", "")).upper() == "USDT":
            continue
        t = coin_broker.held_ticker_row(b)
        if not t:
            continue
        curr_p = coin_broker.get_current_price(t)
        if curr_p:
            qv = float(_to_float(b.get("balance", 0))) * float(curr_p)
            total_coin_equity += qv * kpx if coin_config.is_binance() else qv
    return float(total_coin_equity)


def _count_coin_positions_for_sell_loop(balances, positions: dict) -> int:
    """코인 매도 루프 대상 포지션 개수 집계."""
    n = 0
    for b in balances:
        if b.get("currency") in ["KRW", "VTHO"]:
            continue
        if coin_config.is_binance() and str(b.get("currency", "")).upper() == "USDT":
            continue
        t = coin_broker.held_ticker_row(b)
        if t and t in positions and coin_broker.should_include_coin_balance_row(b):
            n += 1
    return n


def _format_coin_price_log_fields(
    curr_p: float, buy_p: float, max_p: float, chandelier_p: float, hard_stop: float
) -> tuple[str, str, str, str, str]:
    """코인 상태 로그용 가격 포맷 문자열 생성(기존 로직 동일)."""
    curr_fmt = f"{curr_p:,.4f}" if curr_p < 100 else f"{curr_p:,.0f}"
    buy_fmt = f"{buy_p:,.4f}" if buy_p < 100 else f"{buy_p:,.0f}"
    max_fmt = f"{max_p:,.4f}" if max_p < 100 else f"{max_p:,.0f}"
    chan_fmt = f"{chandelier_p:,.4f}" if chandelier_p < 100 else f"{chandelier_p:,.0f}"
    hard_fmt = f"{hard_stop:,.4f}" if hard_stop < 100 else f"{hard_stop:,.0f}"
    return curr_fmt, buy_fmt, max_fmt, chan_fmt, hard_fmt


def _position_buy_anchor_dt(pos_info: dict) -> datetime | None:
    """타임스탑·영업시간 계산용 매수 시각."""
    buy_date_str = pos_info.get("buy_date")
    buy_time_ts = pos_info.get("buy_time")
    if buy_date_str:
        try:
            return datetime.fromisoformat(str(buy_date_str).strip())
        except Exception:
            pass
    if buy_time_ts:
        try:
            return datetime.fromtimestamp(float(buy_time_ts))
        except (TypeError, ValueError, OSError):
            pass
    return None


def _time_stop_hours_elapsed(
    market: str,
    start: datetime | float,
    end: datetime | float | None = None,
) -> float:
    """
    타임스탑·보유시간용 경과 시간(h).

    * COIN: 연속 시간 그대로 (24/7).
    * KR/US: ``start``~``end`` 중 **휴장일(주말·공휴일)만 제외**하고 나머지는 24시간 기준으로 연속 누적.
      - 예: 월 15:00 매수 → 화 장 열림이면 **그 사이(밤 포함)** 는 계속 누적.
      - 수 휴장이면 수요일 24h는 누적에서 제외(Pause).
    """
    m = str(market or "").strip().upper()
    if isinstance(start, (int, float)):
        start_dt = datetime.fromtimestamp(float(start))
    else:
        start_dt = start
    if end is None:
        end_dt = datetime.now()
    elif isinstance(end, (int, float)):
        end_dt = datetime.fromtimestamp(float(end))
    else:
        end_dt = end

    if end_dt <= start_dt:
        return 0.0

    if m == "COIN":
        return max(0.0, (end_dt - start_dt).total_seconds() / 3600.0)

    # KR/US: "휴장일만 Pause" (개장일은 장외 포함 24h 연속 누적)
    open_dates: set = set()
    try:
        cal_name = "XKRX" if m == "KR" else "NYSE"
        cal = mcal.get_calendar(cal_name)
        # 거래일만 반환(주말·공휴일 제외)
        valid = cal.valid_days(start_date=start_dt.date(), end_date=end_dt.date())
        open_dates = {pd.Timestamp(x).date() for x in valid}
    except Exception:
        # 폴백: 주말만 거래일 취급(공휴일은 모름)
        d = start_dt.date()
        last = end_dt.date()
        while d <= last:
            if d.weekday() < 5:
                open_dates.add(d)
            d = d + timedelta(days=1)

    total_sec = 0.0
    cur = start_dt
    while cur < end_dt:
        day_start = datetime(cur.year, cur.month, cur.day)
        day_end = day_start + timedelta(days=1)
        seg_end = min(day_end, end_dt)
        if cur.date() in open_dates:
            total_sec += max(0.0, (seg_end - cur).total_seconds())
        cur = seg_end
    return max(0.0, total_sec / 3600.0)


def _compute_holding_time_info(
    pos_info: dict, market: str = ""
) -> tuple[str, float, float, str]:
    """(now_str, calendar_hours, trading_hours_for_time_stop, buy_time_log)."""
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    buy_dt = _position_buy_anchor_dt(pos_info)
    buy_time_log = "알 수 없음"
    calendar_h = 0.0
    trading_h = 0.0

    if buy_dt is not None:
        buy_time_log = buy_dt.strftime("%Y-%m-%d %H:%M:%S")
        calendar_h = max(0.0, (now - buy_dt).total_seconds() / 3600.0)
        m = str(market or "").strip().upper()
        if m in ("KR", "US", "COIN"):
            trading_h = _time_stop_hours_elapsed(m, buy_dt, now)
        else:
            trading_h = calendar_h

    return now_str, float(calendar_h), float(trading_h), buy_time_log


def _print_position_hold_status(
    now_str: str,
    ticker: str,
    buy_time_log: str,
    hours_held: float,
    *,
    line_prefix: str = "",
    trading_hours: float | None = None,
    market: str = "",
) -> None:
    """보유시간 상태 로그 — 타임스탑과 동일한 누적 시간만 표시."""
    print(f"{line_prefix}📊 [{now_str}] {ticker} 상태 체크")
    disp_h = (
        float(trading_hours)
        if trading_hours is not None
        else float(hours_held)
    )
    print(f"{line_prefix}   ⏱️ 매수일시: {buy_time_log} ➔ 보유: {disp_h:.1f}h")


def _position_qty_for_heat(ticker: str, pos: dict) -> float:
    try:
        return float(pos.get("qty", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _portfolio_heat_snapshot(
    state: dict,
    market: str,
    market_equity: float,
    fetch_ohlcv,
    *,
    extra_weight: float = 0.0,
    extra_atr_pct: float = 0.0,
) -> tuple[float, bool]:
    """(heat 비율, 차단 여부)."""
    heat = compute_market_portfolio_heat(
        state.get("positions", {}) or {},
        market,
        float(market_equity),
        resolve_market=_market_from_ticker,
        position_qty=_position_qty_for_heat,
        fetch_ohlcv=fetch_ohlcv,
        extra_weight=float(extra_weight),
        extra_atr_pct=float(extra_atr_pct),
    )
    blocked = portfolio_heat_blocks_entry(heat, PORTFOLIO_HEAT_MAX_PCT)
    return float(heat), bool(blocked)


def _log_portfolio_heat_block(market: str, heat: float, *, prospective: bool = False) -> None:
    tag = "신규 포함" if prospective else "현재"
    print(
        f"  🚫 [{market} Portfolio Heat] {tag} Heat {heat * 100:.2f}% "
        f"≥ 한도 {PORTFOLIO_HEAT_MAX_PCT * 100:.1f}% — 신규 매수 차단 (V8·스윙 공통)"
    )


def _register_swing_risk_after_buy(
    state: dict,
    ticker: str,
    ohlcv,
    market: str,
) -> None:
    """SWING_FIB 체결 직후 1R·초기 하드 바닥을 장부에 기록."""
    pos = (state.get("positions") or {}).get(ticker)
    if not pos or str(pos.get("strategy_type", "")).upper() != "SWING_FIB":
        return
    buy_p = float(_to_float(pos.get("buy_p", 0), 0.0))
    if buy_p <= 0:
        return
    register_swing_entry_risk_fields(pos, buy_p, ohlcv, market=market, ticker=ticker)
    state.setdefault("positions", {})[ticker] = pos
    save_state(STATE_PATH, state)


def _iter_coin_asset_rows(balances):
    """balances에서 표시통화·더스트 제외 코인 row만 순회."""
    for b in balances:
        if b.get("currency") in ["KRW", "VTHO"]:
            continue
        if coin_config.is_binance() and str(b.get("currency", "")).upper() == "USDT":
            continue
        yield b


def _build_coin_ohlcv_from_upbit_df(df_upbit) -> list[dict]:
    """Upbit OHLCV DataFrame -> 내부 표준 ohlcv(list[dict]) 변환."""
    return [
        {"o": row["open"], "h": row["high"], "l": row["low"], "c": row["close"], "v": row["volume"]}
        for _, row in df_upbit.iterrows()
    ]


def _resolve_curr_price_with_gui_override(pos_info: dict, curr_p: float) -> float:
    """장부 공유 현재가(curr_p)가 유효하면 우선 적용(기존 동작 동일)."""
    gui_p = pos_info.get("curr_p")
    if gui_p and float(gui_p) > 0:
        return float(gui_p)
    return float(curr_p)


def _calc_profit_rate_pct(curr_p: float, buy_p: float) -> float:
    """수익률(%) 계산 공통식."""
    return ((curr_p - buy_p) / buy_p) * 100 if buy_p > 0 else 0.0


def _update_position_current_atr_if_changed(state: dict, ticker: str, pos_info: dict, atr_val) -> None:
    """ATR가 유효하고 변경됐을 때만 장부 반영/저장(기존 동작 동일)."""
    if atr_val is None:
        return
    prev_atr = _to_float(pos_info.get("current_atr", 0), 0.0)
    if abs(prev_atr - float(atr_val)) > 1e-9:
        pos_info["current_atr"] = float(atr_val)
        state.setdefault("positions", {})[ticker] = pos_info
        save_state(STATE_PATH, state)


def _market_from_ticker(ticker: str) -> str:
    """티커로 KR / US / COIN 구분."""
    t = str(ticker or "").strip()
    if is_coin_ticker(t):
        return "COIN"
    if t.isdigit():
        return "KR"
    return "US"


def _calc_hard_stop(
    pos_info: dict,
    buy_p: float,
    *,
    ohlcv=None,
    strategy_type: str = "",
    ticker: str = "",
    trading_hours_held: float | None = None,
) -> float:
    """포지션 하드스탑 — 스윙: 피보·구름·시간가중 / V8: ``sl_p``·90% 폴백."""
    st = str(strategy_type or pos_info.get("strategy_type") or "TREND_V8").upper()
    if st == "SWING_FIB":
        m = _market_from_ticker(ticker)
        th = trading_hours_held
        if th is None:
            _, _, th, _ = _compute_holding_time_info(pos_info, m)
        hard = float(
            get_swing_hard_stop_floor(
                pos_info,
                ohlcv,
                market=m,
                ticker=ticker,
                trading_hours_held=th,
            )
        )
        if hard > 0:
            return hard
        return 0.0
    return float(pos_info.get("sl_p", buy_p * 0.9))


def _v8_loss_stop_is_breakeven_lock(buy_p: float, pos_info: dict, hard_stop: float) -> bool:
    """V8 손실 구간 — 1차 분할 익절 후 본절 락(+0.5%)이 매도선에 반영됐으면 True."""
    bp = float(buy_p or 0)
    hs = float(hard_stop or 0)
    if bp <= 0 or hs <= 0:
        return False
    lock_floor = float(get_v8_profit_lock_floor(bp, pos_info or {}))
    if lock_floor <= 0:
        return False
    return hs >= lock_floor * 0.998


def _v8_loss_stop_log_label(buy_p: float, pos_info: dict, hard_stop: float) -> str:
    return "본절락" if _v8_loss_stop_is_breakeven_lock(buy_p, pos_info, hard_stop) else "손절가"


def _v8_loss_zone_exit_meta(
    buy_p: float,
    pos_info: dict,
    hard_stop: float,
    curr_p: float,
    *,
    market: str = "KR",
) -> tuple[str, str]:
    """V8 손실 구간 청산 — (텔레·장부 사유, 콘솔 태그)."""
    mk = str(market or "KR").strip().upper()
    hs = float(hard_stop or 0)
    cp = float(curr_p or 0)
    if _v8_loss_stop_is_breakeven_lock(buy_p, pos_info, hs):
        reason = "본절락 이탈 (1차 분할 익절 후 +0.5% 방어선)"
        tag = "본절락"
    else:
        reason = "하드스탑 이탈 (손실구간 방어)"
        tag = "하드스탑"
    if mk == "US":
        log = f"🔴 [{tag} 발동] 현재가: ${cp:.2f} <= 기준 ${hs:.2f}. 강제 청산!"
    elif mk == "COIN":
        if cp < 100:
            log = f"🔴 [{tag} 발동] {cp:.4f} <= 기준 {hs:.4f}. 강제 청산!"
        else:
            log = f"🔴 [{tag} 발동] {cp:,.0f} <= 기준 {hs:,.0f}. 강제 청산!"
    else:
        log = f"🔴 [{tag} 발동] 현재가: {cp:,.0f}원 <= 기준 {hs:,.0f}원. 강제 청산!"
    return reason, log


def _kis_post_trade_balance_pause() -> None:
    """매도·체결 직후 KIS 잔고 조회 전 대기 — 초당 한도(EGW00201) 완화."""
    import os

    sec = float(os.environ.get("BOT_KIS_POST_SELL_DELAY_SEC", "2.5"))
    if sec > 0:
        time.sleep(sec)


def _update_position_max_p(state: dict, ticker: str, pos_info: dict, curr_p: float) -> float:
    """최고가(max_p) 갱신 — 스윙·V8 수익 락·매도선 표시에 사용."""
    buy_p = float(_to_float(pos_info.get("buy_p", curr_p), curr_p))
    old = float(_to_float(pos_info.get("max_p", 0), 0.0))
    new_max = max(old or buy_p, float(curr_p))
    if new_max > old:
        pos_info["max_p"] = new_max
        state.setdefault("positions", {})[ticker] = pos_info
    return float(pos_info.get("max_p", new_max))


def _resolve_exit_display_price(
    ticker: str,
    curr_p: float,
    pos_info: dict,
    ohlcv,
    strategy_type: str,
    *,
    state: dict | None = None,
    trading_hours_held: float | None = None,
) -> float:
    """V8 샹들리에+콘크리트 또는 SWING_FIB 전용 매도선."""
    st = str(strategy_type or "TREND_V8").upper()
    if st == "SWING_FIB":
        if reconcile_swing_position(pos_info, ohlcv, reference_price=float(curr_p)):
            if state is not None and ticker:
                state.setdefault("positions", {})[ticker] = pos_info
                save_state(STATE_PATH, state)
        m = _market_from_ticker(ticker)
        th = trading_hours_held
        if th is None:
            _, _, th, _ = _compute_holding_time_info(pos_info, m)
        return float(
            get_swing_exit_display_price(
                curr_p,
                pos_info,
                ohlcv,
                market=m,
                ticker=ticker,
                trading_hours_held=th,
            )
        )
    return float(get_final_exit_price(ticker, curr_p, pos_info, ohlcv))


def _persist_exit_line_sl_p(
    state: dict, ticker: str, pos_info: dict, exit_line: float
) -> None:
    if exit_line > 0:
        prev = float(_to_float(pos_info.get("sl_p", 0), 0.0))
        st = str(pos_info.get("strategy_type") or "").strip().upper()
        tier = str(pos_info.get("tier") or "").strip().upper()
        if st == "SWING_FIB" or tier in ("SWING_FIB", "SWING"):
            if prev > 0:
                exit_line = max(prev, float(exit_line))
        pos_info["sl_p"] = float(exit_line)
        state.setdefault("positions", {})[ticker] = pos_info


def _format_swing_exit_log_suffix(
    market: str, pos_info: dict, ohlcv, curr_p: float, buy_p: float
) -> str:
    """스윙 보유 로그: 1.5R 1차 익절 목표가."""
    half = get_swing_scale_out_target_price(pos_info)
    if half is None or half <= 0:
        return ""
    if market == "US":
        return f" | 1차익절({SWING_SCALE_OUT_R_MULT:.1f}R): ${half:.2f}"
    if market == "COIN":
        if half < 100:
            return f" | 1차익절({SWING_SCALE_OUT_R_MULT:.1f}R): {half:,.4f}"
        return f" | 1차익절({SWING_SCALE_OUT_R_MULT:.1f}R): {half:,.0f}"
    return f" | 1차익절({SWING_SCALE_OUT_R_MULT:.1f}R): {int(half):,}"


def _check_swing_trailing_exit(
    curr_p: float, pos_info: dict, ohlcv, state: dict, ticker: str
) -> tuple[bool, str]:
    """스윙 트레일링 — 비러너 본절 락 / 러너 5MA 이탈. 하드·5MA FULL은 ``check_swing_exit`` 와 연동."""
    m = _market_from_ticker(ticker)
    exit_line = get_swing_exit_display_price(
        curr_p, pos_info, ohlcv, market=m, ticker=ticker
    )
    _persist_exit_line_sl_p(state, ticker, pos_info, exit_line)
    return check_swing_profit_lock_trailing_exit(
        curr_p, pos_info, ohlcv=ohlcv, market=m, ticker=ticker
    )


# 타임스탑 — KR/US는 **영업시간** 누적, COIN은 24/7 연속시간
#   V8 주식: 72h(≈3영업일) + 유예 +4% | V8 코인: 48h + 유예 +4%
#   SWING 주식: 48h + 유예 +2% | SWING 코인: 24h + 유예 +2%
# (보유시각: buy_date 우선, 없으면 buy_time)
V8_TIME_STOP_HOURS_EQUITY = 72.0
V8_TIME_STOP_HOURS_COIN = 48.0
V8_TIME_STOP_EXEMPT_PROFIT_PCT = 4.0
SWING_TIME_STOP_HOURS_EQUITY = 48.0
SWING_TIME_STOP_HOURS_COIN = 24.0
SWING_TIME_STOP_EXEMPT_PROFIT_PCT = 2.0


def _time_stop_params(market: str, strategy_type: str) -> tuple[str, float, float]:
    """(로그 태그, 최소 보유 시간(시간), 유예 수익률 임계 %)."""
    m = (market or "").upper()
    st = (strategy_type or "TREND_V8").upper()
    if st == "SWING_FIB":
        if m == "KR":
            return "[SWING_TIME_STOP_KR]", SWING_TIME_STOP_HOURS_EQUITY, SWING_TIME_STOP_EXEMPT_PROFIT_PCT
        if m == "US":
            return "[SWING_TIME_STOP_US]", SWING_TIME_STOP_HOURS_EQUITY, SWING_TIME_STOP_EXEMPT_PROFIT_PCT
        return "[SWING_TIME_STOP_COIN]", SWING_TIME_STOP_HOURS_COIN, SWING_TIME_STOP_EXEMPT_PROFIT_PCT
    if m == "COIN":
        return "[V8_TIME_STOP_COIN]", V8_TIME_STOP_HOURS_COIN, V8_TIME_STOP_EXEMPT_PROFIT_PCT
    if m == "KR":
        return "[V8_TIME_STOP_KR]", V8_TIME_STOP_HOURS_EQUITY, V8_TIME_STOP_EXEMPT_PROFIT_PCT
    return "[V8_TIME_STOP_US]", V8_TIME_STOP_HOURS_EQUITY, V8_TIME_STOP_EXEMPT_PROFIT_PCT


def _evaluate_time_stop(
    *,
    market: str,
    strategy_type: str,
    hours_held: float,
    profit_rate_now: float,
) -> tuple[bool, str, bool]:
    """
    (전량 청산 여부, 사유 문자열, 유예 로그 출력 여부)
    유예: 보유시간은 임계 초과였으나 수익률이 유예 기준 이상.
    """
    tag, min_h, exempt_pct = _time_stop_params(market, strategy_type)
    hh = float(hours_held)
    pr = float(profit_rate_now)
    if hh < float(min_h):
        return False, "", False
    if pr >= float(exempt_pct):
        return False, "", True
    reason = (
        f"{tag} 타임스탑 — 보유 {hh:.1f}h (≥{min_h:.0f}h), 수익률 {pr:+.2f}% "
        f"< 유예 {exempt_pct:.1f}% → 전량 매도"
    )
    return True, reason, False


def _new_buy_protection_remaining_sec(buy_time) -> int:
    """신규 매수 보호 구간(15분) 남은 시간(초)."""
    elapsed = time.time() - buy_time if buy_time else 900
    remain = int(900 - elapsed)
    return remain if remain > 0 else 0


def _new_buy_sell_protection_blocks(profit_rate_pct: float, buy_time) -> bool:
    """매수 후 15분·수익률 +1% 미만이면 매도 판정 스킵 (손실 구간 포함, KR/US/COIN 공통)."""
    if _new_buy_protection_remaining_sec(buy_time) <= 0:
        return False
    return float(profit_rate_pct) < 1.0


def _ai_false_breakout_buy_gate(
    ticker: str,
    market_tag: str,
    strategy_type: str,
    threshold: int,
    log_label: str,
) -> bool:
    """Phase 3 뉴스 악재 필터. True = 통과(매수 진행), False = 차단.

    ``strategy_type`` — ``TREND_V8`` / ``SWING_FIB`` 등을 ``ai_filter``에 넘겨 듀얼 프롬프트 분기.
    """
    if not AI_FALSE_BREAKOUT_ENABLED:
        return True
    if _is_hedge_ticker(ticker, market_tag):
        print(
            f"  [AI PASS] {ticker} - 헷지 자산 (Phase3 필터 생략, false_breakout_prob=0)"
        )
        return True
    st = str(strategy_type or "TREND_V8").upper()
    ai_eval = evaluate_false_breakout_filter(
        ticker=ticker,
        market=market_tag,
        threshold=int(threshold),
        use_ai=True,
        ai_provider=AI_FALSE_BREAKOUT_PROVIDER,
        config=config,
        strategy_type=st,
    )
    prob = int(ai_eval.get("false_breakout_prob", 0) or 0)
    eng = str(ai_eval.get("evaluation_engine", "?"))
    profile = str(ai_eval.get("prompt_profile", "") or "")
    if ai_eval.get("openai_fallback_used"):
        eng = f"{eng} (Gemini→OpenAI 폴백)"
    summ = summarize_ai_rationale(str(ai_eval.get("rationale", "")))
    profile_txt = f" | 프롬프트: {profile}" if profile else ""
    if ai_eval.get("blocked"):
        print(
            f"  ⏭️ {log_label}: [AI FILTER] 위험도 {prob}% ≥ {int(threshold)}% | "
            f"전략: {st}{profile_txt} | 산출: {eng} | 사유: {summ}"
        )
        return False
    print(
        f"  [AI PASS] {ticker} - 전략: {st}{profile_txt} | 위험도: {prob}점 | "
        f"산출: {eng} | 사유: {summ}"
    )
    return True


def _run_kr_buy_cycle(
    ctx,
    *,
    state: dict,
    weather: dict,
    macro_mult: float,
    macro_snap: dict,
    held_kr,
    kr_cash: int,
    total_kr_equity: float,
    buy_cycle_tag: str,
    alpha_target_vol: float,
) -> int:
    """국장 매수 루프 — ``execution.market_cycles.kr_buy_cycle`` 위임 (하위 호환)."""
    from execution.market_cycles.kr_buy_cycle import run_kr_buy_cycle

    return run_kr_buy_cycle(
        ctx,
        held_kr=held_kr,
        kr_cash=int(kr_cash),
        total_kr_equity=float(total_kr_equity),
        alpha_target_vol=float(alpha_target_vol),
    )


def _run_us_buy_cycle(
    ctx,
    *,
    state: dict,
    weather: dict,
    macro_mult: float,
    macro_snap: dict,
    held_us,
    us_cash: float,
    total_us_equity: float,
    buy_cycle_tag: str,
    alpha_target_vol: float,
) -> float:
    """미장 매수 루프 — ``execution.market_cycles.us_buy_cycle`` 위임 (하위 호환)."""
    from execution.market_cycles.us_buy_cycle import run_us_buy_cycle

    return run_us_buy_cycle(
        ctx,
        held_us=held_us,
        us_cash=float(us_cash),
        total_us_equity=float(total_us_equity),
        alpha_target_vol=float(alpha_target_vol),
    )


def _run_coin_buy_cycle(
    ctx,
    *,
    coin_weather: str,
    held_coins,
    krw_bal: float,
    total_coin_equity: float,
    alpha_target_vol: float,
) -> float:
    """코인 매수 루프 — ``execution.market_cycles.coin_buy_cycle`` 위임 (하위 호환)."""
    from execution.market_cycles.coin_buy_cycle import run_coin_buy_cycle

    return run_coin_buy_cycle(
        ctx,
        coin_weather=str(coin_weather),
        held_coins=held_coins,
        krw_bal=float(krw_bal),
        total_coin_equity=float(total_coin_equity),
        alpha_target_vol=float(alpha_target_vol),
    )


def run_trading_bot():
    """
    한 번의 **트레이딩 사이클**을 수행한다 (스케줄러가 주기적으로 호출).

    순서 개요
        1) ``bot_state`` 로드·키 정규화, 브로커 토큰 갱신.
        2) 실계좌 vs 장부 ``sync_all_positions`` (국·미 보유는 정규장에만 KIS 갱신; 코인·장부는 매 틱).
        3) 시장 날씨·거시 방어막·Phase5 월요일 주차 고점·합산 서킷(MDD).
        4) 보유 종목 위주 손절/익절·(조건 시) TWAP 청산 등.
        5) 스크리너 후보에 대해 섹터락·AI필터·매수 윈도·TWAP 매수.

    매수 스킵 로그 태그(grep 용)
        * ``[KR 예산 부족]`` / ``[KR 예수금 부족]`` / ``[KR 매수 스킵]`` / ``[KR 매수 미체결]``
        * ``[US 예산 부족]`` / ``[US 예수금 부족]`` / ``[US 매수 스킵]`` / ``[US 매수 미체결]`` / ``[US TWAP]``
        * ``[COIN 예산 부족]`` / ``[COIN 예수금 부족]`` / ``[COIN 매수 미체결]`` / ``[COIN TWAP]``

    주의
        장시간 블로킹 호출(API·yfinance)이 포함되므로, 호출 주기와 겹치지 않게 설정한다.
    """
    print("\n" + "="*55)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🤖 V5.0 통합 자동매매 봇 가동...")
    print("="*55)

    state = _prepare_cycle_state()
    _sync_positions_for_cycle(state)
    weather, macro_mult, macro_reason, macro_snap = _build_market_context(state)
    _alpha_target_vol = float(config.get("alpha_target_vol", 0.02))
    state = load_state(STATE_PATH)
    _buy_cycle_tag = order_idem.cycle_tag_15m_kst()
    _rec_fixes = order_idem.reconcile_positions_for_cycle(state, _buy_cycle_tag, STATE_PATH)
    if _rec_fixes > 0:
        print(f"  🔧 [장부 정합] 이번 사이클 filled↔positions 보정 {_rec_fixes}건")

    _cycle_buy_fills = 0
    _cycle_buy_zone_kr = False
    _cycle_buy_zone_us = False
    _cycle_buy_zone_coin = False

    try:
        with open(BASE_DIR / "kr_targets.json", "r", encoding="utf-8") as f:
            scanned_targets = json.load(f)
    except Exception as _e_kr_targets:
        scanned_targets = []
        print(f"  ⚠️ [국장 타겟] kr_targets.json 로드 실패 — 빈 스캔 목록으로 진행: {_e_kr_targets}")

    # 1) 국장 타겟 구성 (조건검색 베이스 필터링)
    market_cap_200 = get_kis_market_cap_rank(kis_api.broker_kr, limit=200)
    time.sleep(1.0)
    realtime_trade_all = get_kis_top_trade_value(kis_api.broker_kr)
    
    # 편의를 위해 거래대금 상위 50위까지만 컷
    top_vol_50 = realtime_trade_all[:50] 

    final_targets = _build_kr_targets(scanned_targets, market_cap_200, top_vol_50)
    final_targets = _merge_hedge_into_buy_targets(final_targets, "KR")
    final_targets = _sort_buy_targets_by_rs(final_targets, "KR")

    # -------------------------------------------------------------------------
    # 시장별 엔진 — execution/market_cycles (B-1 분리, 로직·순서 동일)
    # -------------------------------------------------------------------------
    from execution.market_cycles import TradingCycleContext, run_coin_cycle, run_kr_cycle, run_us_cycle

    _cycle_ctx = TradingCycleContext(
        state=state,
        weather=weather,
        macro_mult=macro_mult,
        macro_reason=macro_reason,
        macro_snap=macro_snap,
        buy_cycle_tag=_buy_cycle_tag,
        final_targets_kr=final_targets,
    )
    run_kr_cycle(_cycle_ctx)
    run_us_cycle(_cycle_ctx)
    run_coin_cycle(_cycle_ctx)
    _cycle_buy_fills = _cycle_ctx.buy_fills
    _cycle_buy_zone_kr = _cycle_ctx.buy_zone_kr
    _cycle_buy_zone_us = _cycle_ctx.buy_zone_us
    _cycle_buy_zone_coin = _cycle_ctx.buy_zone_coin


    if (_cycle_buy_zone_kr or _cycle_buy_zone_us or _cycle_buy_zone_coin) and _cycle_buy_fills == 0:
        _buy_pass_zones = []
        if _cycle_buy_zone_kr:
            _buy_pass_zones.append("KR")
        if _cycle_buy_zone_us:
            _buy_pass_zones.append("US")
        if _cycle_buy_zone_coin:
            _buy_pass_zones.append("COIN")
        _buy_pass_zone_label = "·".join(_buy_pass_zones)
        send_telegram(
            f"📭 [매수 패스] 이번 사이클: {_buy_pass_zone_label} 매수 가능 시간·조건 구간이었으나 "
            "신규 매수 체결이 없었습니다.\n"
            "(시그널 없음·AI 필터·예산·최소주문·TWAP 미체결 등)"
        )

    save_state(STATE_PATH, state)
    print("="*60)


def _kst_minute_is_half_hour_mark() -> bool:
    """KST 벽시계 :00 / :30 슬롯 여부."""
    now = datetime.now(pytz.timezone("Asia/Seoul"))
    return int(now.minute) in (0, 30)


def run_trading_bot_maybe_heartbeat(with_heartbeat: bool = False):
    """KST 분봉 매매 사이클 실행. ``with_heartbeat`` 이면 사이클 종료 후 생존신고."""
    run_trading_bot()
    if with_heartbeat:
        heartbeat_report()


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

    held_kr, held_us = fetch_equity_held_lists_for_position_sync()
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

    run_trading_bot_maybe_heartbeat(with_heartbeat=_kst_minute_is_half_hour_mark())

    # 매매 사이클: KST 벽시계 :00 / :15 / :30 / :45 (기동 직후 위에서 1회 이미 실행됨)
    schedule.clear("trading")
    for minute_mark in (":00", ":15", ":30", ":45"):
        with_heartbeat = minute_mark in (":00", ":30")
        job = lambda hb=with_heartbeat: run_trading_bot_maybe_heartbeat(with_heartbeat=hb)
        try:
            schedule.every().hour.at(minute_mark, "Asia/Seoul").do(job).tag("trading")
        except TypeError:
            schedule.every().hour.at(minute_mark).do(job).tag("trading")

    start_scanner_scheduler()

    run_continuously()
    print("\n✅ 모든 시스템이 정상적으로 가동되었습니다.")
    print("  [스케줄] 매매: 매시 KST :00 / :15 / :30 / :45 + 기동 직후 1회 (생존신고: :00·:30 사이클 종료 후)")

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
