# -*- coding: utf-8 -*-
"""
PyQt5 운영 GUI — ``run_bot`` 엔진을 탭·QTimer·스레드로 감싼다.

특징
    * 잔고·성적표·수동 매도·로그 뷰 등은 ``run_bot`` / ``execution`` / ``utils`` API를 그대로 호출.
    * **실시간 작동 로그(봇 브리핑)** 는 탭 위젯 **아래**에 두어, 탭을 바꿔도 같은 자리에 보이게 한다(세로 ``QSplitter``).
    * ``import run_bot`` 시점에 ``config.json`` 이 로드되므로 **설정 변경 후 GUI 재시작** 필요.
    * **고점 보정 (입출금)** 탭: ``adjust_capital.py`` 와 동일하게 ``peak_total_equity``·``capital_adjustments`` 반영 (백그라운드 스레드).
    * 바이낸스 코인 상단 **예수금·총평가** 숫자는 ``binance_display_cash_and_total_usdt()`` 등. 보유/장부 단가는 USDT. 스냅샷·서킷은 엔진과 동일 원화 환산.
    * 매매는 시작 즉시 실행하지 않고, **KST :00 / :15 / :30 / :45** 정렬 스케줄에만 맞춰 `run_trading_bot`을 실행한다.
    * ``QTimer.singleShot`` 겹침으로 로그가 두 줄씩 나오는 것을 막기 위해 **단일 ``QTimer`` + 실행 중 가드**를 쓴다.
    * 네트워크 감시는 **백그라운드 스레드**에서 돌린다. 생존신고(heartbeat) 텔레그램은 **KST :00 / :30** 30분마다 예약하고, **해당 슬롯의 15분 매매 사이클이 끝난 뒤** 보낸다.

로그
    * ``RedirectText`` 가 ``utils.logger.get_quant_logger()`` 로 ``logs/bot.log`` 에도 한 줄씩 넘긴다.
"""
import warnings
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

import logging

for _yn in ("yfinance", "urllib3"):
    logging.getLogger(_yn).setLevel(logging.ERROR)

import os
import re
import sys, json
import socket
import threading
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLabel, QTextEdit, QTableWidget, QTableWidgetItem,
                             QHeaderView, QTabWidget, QTabBar, QMessageBox, QSpinBox, QLineEdit,
                             QRadioButton, QButtonGroup, QSizePolicy, QSplitter, QAbstractItemView)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QThread, QSize
from PyQt5.QtGui import QFont
from pathlib import Path
import traceback
import run_bot
from utils.logger import get_quant_logger
from utils.telegram import send_telegram
from utils.helpers import (
    get_kr_company_name,
    get_us_company_name,
    is_coin_ticker,
    kis_equities_weekend_suppress_window_kst,
    normalize_ticker,
    seconds_until_next_quarter_hour,
    seconds_until_next_half_hour,
)
from execution.sync_positions import sync_all_positions, _last_buy_price_from_trade_history
from services.gui_table_adapter import build_rows_data
from run_bot import (
    get_us_cash_real,
    get_held_stocks_us_detail,
    get_held_stocks_kr_info,
    get_held_stocks_us_info,
    get_held_stocks_coins_info,
    load_last_kis_display_snapshot,
    save_last_kis_display_snapshot,
    STATE_PATH,
    manual_sell,
    kr_name_dict,
    us_name_dict,
    heartbeat_report,
    get_safe_balance,
    _calc_kr_holdings_metrics,
    _calc_us_holdings_metrics,
    _calc_coin_holdings_metrics,
    get_balance_with_retry,
    get_us_positions_with_retry,
    refresh_brokers_if_needed,
    _to_float,
    _holding_duration_human,
    build_holding_display_bundle,
    resolve_holding_display_price,
)
from execution.guard import load_state, save_state
from execution import ledger_apply as ledger_apply
from strategy.rules import (
    SWING_ENTRY_RSI_MIN,
    SWING_GAP_UP_MAX_PCT,
    SWING_MA60_MAX_EXTENSION_PCT_COIN,
    SWING_MA60_MAX_EXTENSION_PCT_KR,
    SWING_MA60_MAX_EXTENSION_PCT_US,
    SWING_PROFIT_LOCK_ACTIVATE_PCT,
    SWING_RUNNER_TRAIL_MA_DAYS,
    SWING_SCALE_OUT_R_MULT,
    SWING_TIME_DECAY_GAP_CLOSE_PER_24H,
    SWING_TIME_DECAY_START_TRADING_HOURS,
    SWING_UPPER_WICK_DROP_PCT,
    V8_PROFIT_LOCK_ACTIVATE_PCT,
    get_swing_exit_display_price,
    reconcile_swing_position,
)

TRADE_HISTORY_PATH = Path(__file__).resolve().parent / "trade_history.json"
TRADE_HISTORY_SECTOR_OVERLAY_PATH = Path(__file__).resolve().parent / "trade_history_sectors_backfill.json"

# Internet watch: TCP로 외부망을 짧게 확인. 연속 실패 시에만 quit → run_bot.bat가 GUI 재실행.
# (구버전: 12초×3회 ≈ 40초 끊김에도 종료) — 환경 변수로 조절 가능.
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


_DEFAULT_NET_INTERVAL_MS = 15_000
_DEFAULT_NET_FAILS = 20
_NET_CHECK_INTERVAL_MS = max(5_000, _env_int("BOT_NET_WATCH_INTERVAL_MS", _DEFAULT_NET_INTERVAL_MS))
_NET_FAILS_BEFORE_EXIT = max(3, _env_int("BOT_NET_WATCH_FAILS_BEFORE_EXIT", _DEFAULT_NET_FAILS))
_KIS_REFRESH_MIN_INTERVAL_SEC = 25.0
_last_kis_kr_fetch_ts = 0.0
_last_kis_us_fetch_ts = 0.0


def _internet_reachable(timeout=1.0):
    """TCP connect to public resolvers; no HTTP, no extra deps. 짧은 timeout으로 UI 블로킹 시간 상한을 줄인다."""
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False

def _safe_num(value, default=0.0):
    try:
        if isinstance(value, tuple) and value:
            value = value[0]
        return _to_float(value, default)
    except Exception:
        return float(default)


def _gui_krw_to_usdt_label(krw_int: int, krw_per_usdt: float | None = None) -> str:
    """스냅샷 코인 라벨 값은 내부적으로 항상 원화 환산 정수 — GUI만 USDT로 표시.

    ``krw_per_usdt`` 를 넘기면 예수·총평에 **동일 환율**을 쓴다(두 줄이 같은 스냅샷 기준).
    """
    from api import coin_broker

    r = float(krw_per_usdt) if krw_per_usdt is not None and float(krw_per_usdt) > 0 else float(
        coin_broker.get_krw_per_usdt() or 0.0
    )
    if r <= 0:
        r = 1.0
    u = float(krw_int) / r
    return f"{u:,.2f} USDT"


def _gui_coin_unit_price_str(usdt: float) -> str:
    """보유/장부 테이블: 코인 매수가·현재가 한 줄 (바이낸스 = USDT 단위 그대로 표시)."""
    x = float(usdt)
    if x >= 1000:
        return f"{x:,.2f} USDT"
    if x >= 1:
        return f"{x:,.4f} USDT"
    if x <= 0:
        return "0 USDT"
    s = f"{x:.8f}".rstrip("0").rstrip(".")
    return f"{s} USDT" if s else "0 USDT"


def _position_strategy_label(pos_info: dict) -> str:
    """장부 전략 열: 사이징 라벨(1/N) 대신 매수 전략명을 표시."""
    tier = str((pos_info or {}).get("tier") or "").strip()
    sizing_labels = {"1/N 고정", "1/N"}
    if tier and tier not in sizing_labels:
        return tier
    st = str((pos_info or {}).get("strategy_type") or "TREND_V8").strip().upper()
    if st == "SWING_FIB":
        return "SWING_FIB"
    return "V6 스나이퍼(수급+MACD+RSI+상투방지)"


def _dashboard_strategy_short(pos_info: dict) -> str:
    """실시간 보유 표 — V8 / 스윙."""
    tier = str((pos_info or {}).get("tier") or "").strip().upper()
    st = str((pos_info or {}).get("strategy_type") or "TREND_V8").strip().upper()
    if st == "SWING_FIB" or tier in ("SWING_FIB", "SWING") or "SWING" in tier:
        return "스윙"
    return "V8"


def _gui_market_code_from_label(market_label: str) -> str:
    s = str(market_label or "")
    if "국장" in s:
        return "KR"
    if "미장" in s:
        return "US"
    if "코인" in s:
        return "COIN"
    return "KR"


def _build_strategy_guide_text() -> str:
    """GUI 매매·전략 안내 탭 — README §8·run_bot 엔진과 동기화된 전체 요약."""
    v8_eq = float(getattr(run_bot, "V8_TIME_STOP_HOURS_EQUITY", 72.0))
    v8_coin = float(getattr(run_bot, "V8_TIME_STOP_HOURS_COIN", 48.0))
    sw_eq = float(getattr(run_bot, "SWING_TIME_STOP_HOURS_EQUITY", 48.0))
    sw_coin = float(getattr(run_bot, "SWING_TIME_STOP_HOURS_COIN", 24.0))
    v8_ex = float(getattr(run_bot, "V8_TIME_STOP_EXEMPT_PROFIT_PCT", 4.0))
    sw_ex = float(getattr(run_bot, "SWING_TIME_STOP_EXEMPT_PROFIT_PCT", 2.0))
    sw_r = float(SWING_SCALE_OUT_R_MULT)
    decay_pct = int(SWING_TIME_DECAY_GAP_CLOSE_PER_24H * 100)
    heat_pct = float(getattr(run_bot, "PORTFOLIO_HEAT_MAX_PCT", 0.06)) * 100.0
    max_kr = int(getattr(run_bot, "MAX_POSITIONS_KR", 3))
    max_us = int(getattr(run_bot, "MAX_POSITIONS_US", 3))
    max_coin = int(getattr(run_bot, "MAX_POSITIONS_COIN", 5))
    buy_win = int(getattr(run_bot, "config", {}).get("buy_window_minutes_before_close", 30))
    return (
        "📘 매매·전략 안내 (V8 · SWING_FIB · Phase 1~5)\n"
        "==========================================\n"
        "README.md §8·run_bot.py 와 동일한 운영 요약입니다.\n\n"
        "■ 0) 한 사이클·시장\n"
        "- KST :00/:15/:30/:45 매매 사이클 (GUI·봇 동일 엔진)\n"
        "- 시장: KR(국장) · US(미장) · COIN(업비트 KRW / 바이낸스 USDT)\n"
        "- 종목마다 진입 순서: 10일 RS 정렬 → V8 먼저 → 실패 시 스윙\n"
        f"- 매수 창: 장·일봉 마감 직전 {buy_win}분 (코인=KST 09:00 일봉 경계 직전, 업비트·바이낸스 동일)\n"
        "- 장부 strategy_type: TREND_V8 | SWING_FIB (GUI·텔레: V8 / 스윙)\n\n"
        "■ 1) 스캔 유니버스 (후보 종목)\n"
        "- KR: HTS 조건검색 전체(kr_targets) + 시총200 ∩ 거래대금50 (14:50 KST screener)\n"
        "  · HTS V8 26.05: 유동성 + (20MA·MACD·RSI 추세 OR 볼밴·RSI 턴어라운드)\n"
        "- US: us_universe_cache 고베타 150 (NDX~90 + S&P 섹터 RR, 15:20 US/Eastern)\n"
        "- COIN: 24h 거래대금 상위 N (config upbit/binance_universe_top, 기본 10)\n"
        "  · 스테이블·페그 코인 자동 제외\n\n"
        "■ 2) 매수 전 공통 게이트 (V8·스윙 공통)\n"
        "- Phase5: 합산 계좌 MDD 서킷 (peak_total_equity, 월요일 주차 고점)\n"
        "- Phase4: 시장별 신규 매수 차단 (macro_mult=1.0, 예산 축소 없음)\n"
        "  · US: SPY Put/Call ≥1.2 → US 전면 차단\n"
        "  · COIN: BTC 고래 롱숏 ≤0.8 → COIN 차단\n"
        "  · KR: 원/달러 모멘텀 ≥1.015 → KR 차단\n"
        "- Phase3: AI 뉴스 LLM (V8=엄격 / 스윙=Terminal Risk만)\n"
        "- Phase1: GICS 섹터 과다 보유 방지 (sector_lock)\n"
        "- Phase2: 매수·분할익절 TWAP (전량 청산은 시장가 1회)\n"
        "  · 매수 멱등: 15분 사이클·슬라이스별 order_key, buy_inflight 중복 차단\n"
        "  · 매도 멱등: sell:{lane}(swing_half/full, scale_out, exit, manual, phase5)\n"
        "  · KIS: rt_cd 실패도 잔고 증감으로 체결 보정 / 바이낸스: clientOrderId\n"
        "  · sell_inflight — 동일 사이클·티커·lane 중복 매도 차단\n"
        "  · 장부 등록 120초 내 동일 BUY 중복 스킵(수량만 병합)\n"
        "  · 잔고: balance_read TTL(체결검증 전용, 조회는 원래 멱등)\n"
        "  · 상세: docs/idempotency/ (PROGRESS·BALANCE_READS·SMOKE_TEST)\n"
        f"- Portfolio Heat: 시장별 Σ(비중×ATR%) < {heat_pct:.1f}% (기본 6%)\n"
        f"- 비중: min(1/N, alpha_target_vol/ATR%) — KR {max_kr} / US {max_us} / COIN {max_coin} 슬롯\n"
        "- BEAR 날씨: V8 신규만 차단, 스윙(SWING_FIB)은 허용\n"
        "- 이미 보유·쿨다운·최소주문·예수금 부족 시 패스\n\n"
        "■ 3) V8 추세 매수 — calculate_pro_signals (strategy_type=TREND_V8)\n"
        "- 최소 120봉 OHLCV (목표 캐시 200봉)\n"
        "- Hurst H<0.45 → 횡보·역추세 차단 (50~100봉 R/S)\n"
        "- 양봉 필수 (종가>시가)\n"
        "- 윗꼬리: 고가-종가 > ATR×0.5 거절\n"
        "- 과열: 종가 > 20MA + 3×ATR 거절\n"
        "- 20MA 위 + 20MA 우상향\n"
        "- 120MA 위 (예외: 당일 거래량≥20일 최고 → 바닥권 턴어라운드 허용)\n"
        "- 장기추세: 50MA>200MA (또는 종가>50MA)\n"
        "- 3중 교차: 거래량>20일평균 · MACD>시그널 · RSI 50~75\n"
        "- 초기 sl_p: max(20MA−ATR, 종가−2×ATR) — 평단 대비 고정% 캡 없음\n"
        "- 국장 추가: V8 루프 갭 +5% 컷 (스윙 갭 +3%와 별도)\n"
        "- 통과 로그: [V8-BUY] / 실패: [V8] … 패스 사유\n\n"
        "■ 4) V8 매도 — get_final_exit_price · check_pro_exit\n"
        "- 루프 순서: 분할익절(Scale-Out) → 타임스탑 → 손실 시 hard_stop(sl_p) → 수익 시 check_pro_exit\n"
        f"- 타임스탑: 주식 {v8_eq:.0f}h / 코인 {v8_coin:.0f}h, 유예 수익 +{v8_ex:.1f}% 이상이면 유예\n"
        "- 매도선 = max(샹들리에, 20MA−ATR·종가−2ATR 기술선, 본절락, 장부 sl_p)\n"
        "  · 샹들리에: max_p − ATR×2.5\n"
        f"  · 본절락: max_p 최고수익 ≥{V8_PROFIT_LOCK_ACTIVATE_PCT:.0f}% → 평단×1.005\n"
        "- 수익 구간: 현재가 ≤ 매도선 → 전량 (락·샹들리에 사유 로그)\n"
        "- 스윙 포지션에는 V8 샹들리에·check_pro_exit 미적용\n\n"
        "■ 5) 스윙 매수 — check_swing_entry (V8 실패 후, SWING_FIB)\n"
        "- 최소 60봉 · Hurst/120MA/V8 3중교차 없음 (눌림목 전용)\n"
        "- 60MA 위 + 시장별 이격 상한 (칼날·과열 추격 차단)\n"
        f"  · US +{SWING_MA60_MAX_EXTENSION_PCT_US:.0f}% / KR +{SWING_MA60_MAX_EXTENSION_PCT_KR:.0f}% / "
        f"COIN +{SWING_MA60_MAX_EXTENSION_PCT_COIN:.0f}%\n"
        f"- 양봉 · 갭<{SWING_GAP_UP_MAX_PCT:.0f}% · 거래량 Dry-up(당일<5일평균)\n"
        f"- 윗꼬리<{SWING_UPPER_WICK_DROP_PCT:.0f}% · RSI(14)≥{SWING_ENTRY_RSI_MIN:.0f}\n"
        "- 피보 38.2/50/61.8% 중 현재가 아래 지지 → entry_fib_level\n"
        "- 통과: [SWING-BUY] · 실패: [스윙] 한 줄 사유\n\n"
        "■ 6) 스윙 매도 — check_swing_exit · get_swing_exit_display_price\n"
        "- 루프: check_swing_exit(FULL/HALF) → Scale-Out → 타임스탑 → 트레일링\n"
        f"- 타임스탑: 주식 {sw_eq:.0f}h / 코인 {sw_coin:.0f}h, 유예 +{sw_ex:.1f}%\n"
        "- FULL: 하드(피보·구름+시간가중) · 러너 5MA 이탈 · RSI +1~10%\n"
        f"- HALF: 수익≥{sw_r:.1f}R → 50% 익절 → 러너 후보\n"
        f"- 시간가중: 영업 {SWING_TIME_DECAY_START_TRADING_HOURS:.0f}h 후 24h마다 gap {decay_pct}% 상향\n"
        f"- 본절락: max_p>{SWING_PROFIT_LOCK_ACTIVATE_PCT:.0f}% → 평단×1.005\n"
        f"- 러너: scale_out 또는 max_p≥{sw_r:.1f}R → 5MA 트레일(고점 래칫, sl_p·표시선 하향 없음)\n"
        "- 비러너 트레일: 본절락 이탈 전량 / 러너: 5MA 이탈 전량\n"
        "- V8식 hard_stop 루프는 스윙에 미적용\n\n"
        "■ 7) 타임스탑·쿨다운\n"
        "- KR/US 보유시간: 거래일 연속(장외 포함), 휴장일 Pause (XKRX/NYSE 캘린더)\n"
        "- COIN: 24/7 연속\n"
        "- 전량 청산 후 ticker_cooldowns: 익절·트레일·Scale-Out 1h / 손절·타임스탑 24h\n"
        "- 분할 익절 후 잔량 있으면 쿨다운 미부여\n\n"
        "■ 8) 데이터·GUI 탭\n"
        "- OHLCV: 메모리·data/ohlcv_cache(3일) → KIS→pykrx/Stooq→yfinance\n"
        "- 매도선·sl_p: 15분마다 재계산 (장부 탭·생존신고 동일)\n"
        "- 실시간 현황: 현재가·수량·전략(V8/스윙)\n"
        "- 장부: 최고가(max_p)·매도선·보유시간(영업시간)\n"
        "- 수동 매도: 수량 입력+버튼 (전량/부분, 부분은 stats 별도 누적)\n"
    )


def _table_cell(text) -> QTableWidgetItem:
    """표 셀 — 잘림 시 툴팁으로 전체 문자열 표시."""
    s = str(text)
    item = QTableWidgetItem(s)
    item.setToolTip(s)
    return item


class _DashboardTabBar(QTabBar):
    """한글 탭 제목이 말줄임·세로 클립 없이 보이도록 너비·높이 힌트."""

    _TAB_WIDTH_PAD = 96
    _TAB_MIN_WIDTH = 178

    def tabSizeHint(self, index: int) -> QSize:
        base = super().tabSizeHint(index)
        text = self.tabText(index) or ""
        fm = self.fontMetrics()
        rect = fm.boundingRect(0, 0, 0, 0, Qt.TextSingleLine, text)
        text_w = max(int(rect.width()), int(fm.horizontalAdvance(text)))
        w = max(self._TAB_MIN_WIDTH, text_w + self._TAB_WIDTH_PAD)
        h = max(int(base.height()), int(rect.height()) + 22)
        return QSize(w, h)


def _configure_main_tab_widget(tabs: QTabWidget) -> None:
    bar = tabs.tabBar()
    bar.setExpanding(False)
    bar.setElideMode(Qt.ElideNone)
    bar.setUsesScrollButtons(True)
    bar.setMinimumHeight(40)
    bar.setStyleSheet("QTabBar::tab { font-size: 11px; }")


def _dashboard_stylesheet() -> str:
    """대시보드 전역 QSS — 동작 변경 없이 색·간격·타이포만 적용."""
    return """
    QMainWindow, QWidget {
        background-color: #0f1218;
        color: #e2e8f0;
    }
    QLabel {
        color: #cbd5e1;
    }
    QLabel#StatsBanner {
        font-size: 15px;
        font-weight: 600;
        color: #e0f2fe;
        background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 #1a2332, stop:1 #152238);
        padding: 10px 14px;
        border-radius: 10px;
        border: 1px solid #334155;
    }
    QLabel#SectionTitle {
        font-size: 13px;
        font-weight: 600;
        color: #94a3b8;
        padding: 2px 0 4px 0;
    }
    QWidget#MarketKr, QWidget#MarketUs, QWidget#MarketCoin {
        background-color: #161b26;
        border: 1px solid #2d3748;
        border-radius: 10px;
    }
    QWidget#MarketKr { border-top: 3px solid #38bdf8; }
    QWidget#MarketUs { border-top: 3px solid #818cf8; }
    QWidget#MarketCoin { border-top: 3px solid #fbbf24; }
    QWidget#MarketKr QLabel, QWidget#MarketUs QLabel, QWidget#MarketCoin QLabel {
        color: #f1f5f9;
        font-size: 12px;
        font-weight: 600;
        padding: 3px 8px;
    }
    QTabWidget::pane {
        border: 1px solid #2d3748;
        border-radius: 8px;
        background: #141820;
        top: -1px;
    }
    QTabBar {
        qproperty-drawBase: 0;
    }
    QTabBar::tab {
        background: #1a2030;
        color: #94a3b8;
        padding: 12px 32px;
        margin-right: 6px;
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
        font-weight: 600;
        min-height: 30px;
        min-width: 178px;
    }
    QTabBar::tab:selected {
        background: #243044;
        color: #38bdf8;
        border-bottom: 2px solid #38bdf8;
    }
    QTabBar::tab:hover:!selected {
        background: #1f2838;
        color: #cbd5e1;
    }
    QTableWidget {
        background-color: #141820;
        alternate-background-color: #181e2a;
        gridline-color: #2a3348;
        color: #e2e8f0;
        border: 1px solid #2d3748;
        border-radius: 8px;
        selection-background-color: #2563eb;
        selection-color: #f8fafc;
    }
    QHeaderView::section {
        background-color: #1e293b;
        color: #94a3b8;
        padding: 6px 8px;
        border: none;
        border-bottom: 2px solid #334155;
        font-weight: 600;
        font-size: 11px;
    }
    QTableWidget::item {
        padding: 4px 6px;
    }
    QPushButton {
        background-color: #243044;
        color: #e2e8f0;
        border: 1px solid #3d4f66;
        border-radius: 8px;
        padding: 8px 14px;
        font-weight: 600;
    }
    QPushButton:hover {
        background-color: #2d3d52;
        border-color: #4b6280;
    }
    QPushButton:pressed {
        background-color: #1a2433;
    }
    QPushButton#BtnRefresh {
        background-color: #1d4ed8;
        border-color: #2563eb;
        color: #f8fafc;
    }
    QPushButton#BtnRefresh:hover { background-color: #2563eb; }
    QPushButton#BtnForceKis {
        background-color: #334155;
        border-color: #475569;
        color: #f1f5f9;
    }
    QPushButton#BtnApplyMax {
        background-color: #0f766e;
        border-color: #14b8a6;
        color: #f0fdfa;
    }
    QPushButton#BtnApplyMax:hover { background-color: #0d9488; }
    QPushButton#BtnCapitalApply {
        background-color: #166534;
        border-color: #22c55e;
        color: #ecfdf5;
    }
    QPushButton#BtnCapitalApply:hover { background-color: #15803d; }
    QPushButton#BtnManualSell {
        background-color: #7f1d1d;
        border-color: #b91c1c;
        color: #fef2f2;
        padding: 4px 10px;
        min-width: 52px;
    }
    QPushButton#BtnManualSell:hover { background-color: #991b1b; }
    QSpinBox, QLineEdit {
        background-color: #1a2030;
        color: #e2e8f0;
        border: 1px solid #3d4f66;
        border-radius: 6px;
        padding: 5px 8px;
        selection-background-color: #2563eb;
    }
    QSpinBox:focus, QLineEdit:focus {
        border-color: #38bdf8;
    }
    QRadioButton {
        color: #cbd5e1;
        spacing: 6px;
    }
    QRadioButton::indicator {
        width: 14px;
        height: 14px;
    }
    QTextEdit#LogConsole {
        background-color: #0c0f14;
        color: #86efac;
        font-family: Consolas, 'Cascadia Mono', 'Malgun Gothic', monospace;
        font-size: 12px;
        border: 1px solid #2d3748;
        border-radius: 8px;
        padding: 6px;
    }
    QSplitter::handle {
        background: #2d3748;
        height: 4px;
        margin: 2px 0;
    }
    QScrollBar:vertical {
        background: #141820;
        width: 10px;
        border-radius: 5px;
    }
    QScrollBar::handle:vertical {
        background: #3d4f66;
        border-radius: 5px;
        min-height: 24px;
    }
    QScrollBar::handle:vertical:hover { background: #4b6280; }
    """


def _configure_holding_table(table: QTableWidget) -> None:
    """실시간 현황 — 종목명은 좁게, 오른쪽(수량·가격·전략·매도)은 남는 폭을 나눠 가짐."""
    _configure_data_table(table, stretch_cols=())
    hdr = table.horizontalHeader()
    hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
    hdr.setSectionResizeMode(1, QHeaderView.Interactive)
    for col in (2, 3, 4, 5):
        hdr.setSectionResizeMode(col, QHeaderView.Stretch)
    hdr.setSectionResizeMode(6, QHeaderView.Interactive)
    table.setColumnWidth(1, 200)
    table.setColumnWidth(6, 300)


def _balance_holding_table_columns(table: QTableWidget) -> None:
    """데이터 반영 후 종목명 열을 내용 기준 약 절반으로 캡."""
    if table.rowCount() <= 0:
        table.setColumnWidth(1, 200)
        return
    table.resizeColumnToContents(1)
    natural = int(table.columnWidth(1))
    capped = max(140, min(240, int(natural * 0.52)))
    table.setColumnWidth(1, capped)


def _configure_data_table(table: QTableWidget, *, stretch_cols: tuple[int, ...] = ()) -> None:
    """테이블 공통 — 글자 잘림 방지(말줄임 끔·최소 열너비·가로 스크롤)."""
    table.setTextElideMode(Qt.ElideNone)
    table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
    table.setWordWrap(False)
    table.verticalHeader().setDefaultSectionSize(34)
    hdr = table.horizontalHeader()
    hdr.setMinimumHeight(30)
    hdr.setMinimumSectionSize(76)
    hdr.setDefaultSectionSize(100)
    hdr.setStretchLastSection(False)
    n = table.columnCount()
    stretch_set = set(stretch_cols)
    for col in range(n):
        if col in stretch_set:
            hdr.setSectionResizeMode(col, QHeaderView.Stretch)
        else:
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)


def _ledger_row_key(ticker: str) -> str:
    t = str(ticker or "").strip()
    if t.isdigit():
        return normalize_ticker(t)
    if is_coin_ticker(t):
        return t.upper()
    return normalize_ticker(t)


def _build_live_qty_lookup() -> dict[str, float]:
    """실시간 보유 표와 동일한 실계좌 수량 맵."""
    out: dict[str, float] = {}
    try:
        from api import coin_broker

        for b in coin_broker.get_balances() or []:
            t = coin_broker.held_ticker_row(b)
            if not t:
                continue
            out[str(t).strip().upper()] = float(_to_float(b.get("balance"), 0.0))
    except Exception:
        pass
    try:
        for inf in get_held_stocks_kr_info() or []:
            code = normalize_ticker(str(inf.get("code") or ""))
            q = float(_to_float(inf.get("qty"), 0.0))
            if code and q > 0:
                out[code] = q
    except Exception:
        pass
    try:
        rows = get_held_stocks_us_detail() or []
        if rows:
            for item in rows:
                code = normalize_ticker(str(item.get("code") or ""))
                q = float(_to_float(item.get("qty"), 0.0))
                if code and q > 0:
                    out[code] = q
        else:
            for inf in get_held_stocks_us_info() or []:
                code = normalize_ticker(str(inf.get("code") or ""))
                q = float(_to_float(inf.get("qty"), 0.0))
                if code and q > 0:
                    out[code] = q
    except Exception:
        pass
    return out


def _format_position_qty_for_table(market_label: str, qty: float) -> str:
    q = float(_to_float(qty, 0.0))
    if market_label == "🇰🇷 국장":
        return str(int(round(q)))
    if market_label == "🇺🇸 미장" and abs(q - round(q)) < 1e-6:
        return str(int(round(q)))
    text = f"{q:.8f}".rstrip("0").rstrip(".")
    return text if text else "0"


def get_current_stats(state_file: Path, roi_info=None):
    """STATE_PATH(bot_state.json)에서 성적표 + (옵션) 수익률(%)을 읽어옵니다."""
    roi_info = roi_info or {}
    roi_kr = roi_info.get("KR")
    roi_us = roi_info.get("US")
    roi_coin = roi_info.get("COIN")

    roi_parts = []
    if roi_kr is not None:
        roi_parts.append(f"🇰🇷 {roi_kr:+.2f}%")
    if roi_us is not None:
        roi_parts.append(f"🇺🇸 {roi_us:+.2f}%")
    if roi_coin is not None:
        roi_parts.append(f"🪙 {roi_coin:+.2f}%")
    roi_text = f"   |   📈 수익률: {' / '.join(roi_parts)}" if roi_parts else ""

    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                stats = state.get("stats", {"wins": 0, "losses": 0, "total_profit": 0.0})
                wins = int(stats.get("wins", 0) or 0)
                losses = int(stats.get("losses", 0) or 0)
                total_profit = float(stats.get("total_profit", 0.0) or 0.0)
                total_trades = wins + losses
                win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
                return (
                    f"🏆 승률: {win_rate:.1f}% ({wins}승 {losses}패)"
                    f"   |   💸 누적 수익률 합: {total_profit:.2f}%"
                    f"{roi_text}"
                )
        except Exception:
            pass
    return f"🏆 승률: 데이터 없음   |   💸 누적 수익률 합: 0.00%{roi_text}"

class RedirectText(QObject):
    """표준 출력 리다이렉트. 파일 로깅은 **메인 스레드** ``append_log`` 에서만 수행한다.

    예전 구현은 ``write()`` 안에서 ``logger.info`` 를 호출해, Balancer/하트비트 등 **워커 스레드**가
    ``logs/bot.log``(특히 네트워크 공유 경로)에 동기 쓰기로 묶이거나 락 경쟁으로 멈출 수 있었다.
    시그널은 ``QueuedConnection`` 으로 UI 스레드에만 넘긴다.
    """
    out_signal = pyqtSignal(str)

    def write(self, text):
        if not text:
            return
        try:
            self.out_signal.emit(text)
        except Exception:
            pass

    def flush(self):
        pass

class WorkerThread(QThread):
    def run(self):
        import run_bot
        try:
            run_bot.run_trading_bot()
        except Exception as e:
            print(f"⚠️ 백그라운드 에러 발생: {e}")
            traceback.print_exc()


class CapitalAdjustThread(QThread):
    """``adjust_capital.apply_capital_peak_adjustment`` — 네트워크·API로 UI가 멈추지 않게 백그라운드."""

    done = pyqtSignal(bool, str)

    def __init__(self, withdraw: bool, amount_krw: float, state_path: Path):
        super().__init__()
        self._withdraw = withdraw
        self._amount = float(amount_krw)
        self._state_path = state_path

    def run(self):
        try:
            import adjust_capital

            ok, msg = adjust_capital.apply_capital_peak_adjustment(
                withdraw=self._withdraw,
                amount_krw=self._amount,
                state_path=self._state_path,
                source_label="run_gui.py",
            )
            self.done.emit(ok, msg)
        except Exception as e:
            self.done.emit(False, f"{type(e).__name__}: {e}")


# =====================================================================
# 🚀 [비동기 일꾼] API 통신 및 현재가(야후/업비트) 조회 전담 스레드
# =====================================================================
class BalanceUpdaterThread(QThread):
    # 요리가 다 끝나면 UI 창구로 던져줄 데이터 보따리들
    update_labels = pyqtSignal(dict)  # 라벨 텍스트 데이터
    update_table = pyqtSignal(list)   # 표에 들어갈 행 데이터 리스트
    finished = pyqtSignal()           # 작업 끝남 신호
    error = pyqtSignal(str)           # 에러 신호

    def __init__(self, sync_first=True, dashboard=None, force_kis=False):
        super().__init__()
        self.sync_first = sync_first
        self.dashboard = dashboard  # max_p 업데이트 함수를 쓰기 위해 참조
        self.force_kis = bool(force_kis)

    def run(self):
        try:
            # 아래 테이블 파싱 구간에서 항상 참조하므로 기본값 선초기화
            kr_bal = {}
            us_bal = {}

            def _with_backoff(fetch_fn, label: str, retries: int = 3, delay_sec: float = 1.2):
                """OPSQ0008/MCI 계열 일시 장애 완화용 짧은 백오프 재시도."""
                import time as _t

                last = None
                for i in range(retries):
                    try:
                        out = fetch_fn()
                    except Exception:
                        out = None
                    last = out
                    msg = ""
                    if isinstance(out, dict):
                        msg = str(out.get("msg1", "") or out.get("MSG1", ""))
                    if out and "OPSQ0008" not in msg and "MCI" not in msg:
                        return out
                    if i < retries - 1:
                        print(f"  ⚠️ [{label}] 조회 재시도 {i+1}/{retries-1} (잠시 후 재요청)")
                        _t.sleep(delay_sec)
                return last
            def _allow_kis_fetch(market: str) -> bool:
                """KIS 재조회 허용 여부.

                국·미 예수금·총평 라벨은 **해외/국내 장 마감 후에도** KIS가 잔고를 줄 수 있다.
                ``is_market_open`` 으로 막으면 장중엔 실조회로 맞다가, 장 후에는
                ``last_kis_display_snapshot`` 폴백만 쓰게 되어 **매수 전 달러 예수**처럼 옛 값이
                보일 수 있다. KR/US 각각 **최소 간격(_KIS_REFRESH_MIN_INTERVAL_SEC)** 과
                ``force_kis`` 만으로 호출 수를 제한한다.
                """
                import time as _t
                global _last_kis_kr_fetch_ts, _last_kis_us_fetch_ts
                now_ts = _t.time()
                if self.force_kis:
                    if market == "KR":
                        _last_kis_kr_fetch_ts = now_ts
                    else:
                        _last_kis_us_fetch_ts = now_ts
                    return True
                if market == "KR":
                    if now_ts - float(_last_kis_kr_fetch_ts) < _KIS_REFRESH_MIN_INTERVAL_SEC:
                        return False
                    _last_kis_kr_fetch_ts = now_ts
                    return True
                if market == "US":
                    if now_ts - float(_last_kis_us_fetch_ts) < _KIS_REFRESH_MIN_INTERVAL_SEC:
                        return False
                    _last_kis_us_fetch_ts = now_ts
                    return True
                return False

            # 1. 무거운 장부 동기화 작업
            if self.sync_first:
                try:
                    state = load_state(STATE_PATH)
                    _hk, _hu = run_bot.fetch_equity_held_lists_for_position_sync()
                    _hc = run_bot.get_held_coins()
                    if _hk is not None and _hu is not None and _hc is not None:
                        sync_all_positions(state, _hk, _hu, _hc, STATE_PATH)
                    else:
                        print("  ⚠️ [GUI 장부 동기화 건너뜀] 실보유 조회 실패 — 기존 장부 유지")
                except Exception as e:
                    print(f"⚠️ 장부 동기화 중 오류: {e}")

            # 2~4. KR/US/COIN 스냅샷 공용 fetch (GUI force/쿨다운/백오프 정책 주입)
            if kis_equities_weekend_suppress_window_kst() and not self.sync_first:
                print("💤 [주말 점검] 증권사 API 통신을 건너뛰고 기존 장부를 유지합니다.")
            snap = run_bot.build_account_snapshot_for_report(
                allow_kis_fetch=_allow_kis_fetch,
                with_backoff=_with_backoff,
                force_kis_labels=self.force_kis,
                fresh_balances=True,
            )
            labels = snap.get("labels", {})
            d2_kr = int(_safe_num((labels.get("kr") or {}).get("cash", 0), 0))
            kr_total = int(_safe_num((labels.get("kr") or {}).get("total", 0), 0))
            kr_hold_roi = (labels.get("kr") or {}).get("roi")
            us_cash = float(_safe_num((labels.get("us") or {}).get("cash", 0.0), 0.0))
            us_total = float(_safe_num((labels.get("us") or {}).get("total", 0.0), 0.0))
            us_hold_roi = (labels.get("us") or {}).get("roi")
            krw_cash = int(_safe_num((labels.get("coin") or {}).get("cash", 0), 0))
            coin_total = int(_safe_num((labels.get("coin") or {}).get("total", 0), 0))
            coin_hold_roi = (labels.get("coin") or {}).get("roi")
            bal_map = snap.get("balances", {}) or {}
            kr_bal = bal_map.get("kr") if isinstance(bal_map.get("kr"), dict) else {}
            us_bal = bal_map.get("us") if isinstance(bal_map.get("us"), dict) else {}
            upbit_bals = bal_map.get("coin") if isinstance(bal_map.get("coin"), list) else []

            # 라벨 텍스트 보따리 포장
            labels_data = {
                "kr": {"cash": d2_kr, "total": kr_total, "roi": kr_hold_roi},
                "us": {"cash": us_cash, "total": us_total, "roi": us_hold_roi},
                "coin": {"cash": krw_cash, "total": coin_total, "roi": coin_hold_roi}
            }
            self.update_labels.emit(labels_data)

            # 5. 테이블 행 데이터 수집 및 현재가 실시간 통신 (어댑터 모듈)
            rows_data = build_rows_data(
                kr_bal=kr_bal,
                upbit_bals=upbit_bals,
                is_market_open=run_bot.is_market_open,
                is_weekend_suppress=kis_equities_weekend_suppress_window_kst,
                load_state=load_state,
                state_path=STATE_PATH,
                get_held_stocks_kr_info=get_held_stocks_kr_info,
                get_held_stocks_us_info=get_held_stocks_us_info,
                get_held_stocks_us_detail=get_held_stocks_us_detail,
                kr_name_dict=kr_name_dict,
                us_name_dict=us_name_dict,
                get_kr_company_name=get_kr_company_name,
                get_us_company_name=get_us_company_name,
                safe_num=_safe_num,
                process_row_data=self._process_row_data,
            )

            self.update_table.emit(rows_data)

        except Exception as e:
            self.error.emit(str(e))
            traceback.print_exc()
        finally:
            self.finished.emit()

    def _process_row_data(self, market, name, qty, price, market_code, ticker_code, current_p_api):
        """가장 무거운 현재가 통신(yfinance/upbit)을 백그라운드에서 처리합니다."""
        price_str = str(price).replace('$', '').replace(',', '')
        buy_p = float(price_str)
        current_price, roi = buy_p, 0.0

        ledger_key = ""
        pos: dict = {}
        try:
            m = "KR" if market_code == "KR" else ("US" if market_code == "US" else "COIN")
            # 국·미는 장부 키가 normalize_ticker(6자리·대문자) 기준 — pdno가 int/앞자리0 누락이면 조회 실패해 curr_p가 비었다고 나옴
            ledger_key = (
                normalize_ticker(str(ticker_code))
                if m in ("KR", "US")
                else str(ticker_code).strip().upper()
            )
            st = load_state(STATE_PATH)
            pos = (st.get("positions") or {}).get(ledger_key, {})
            if not isinstance(pos, dict):
                pos = {}
            if m == "COIN" and buy_p <= 0:
                buy_p = float(_to_float(pos.get("buy_p", 0), 0.0))
                if buy_p <= 0:
                    buy_p = float(_last_buy_price_from_trade_history(ledger_key, "COIN") or 0.0)
            current_price = run_bot.resolve_holding_display_price(
                m, ledger_key, buy_p, current_p_api, pos
            )

            # 최고가(max_p)·curr_p 저장은 거래소 라이브 시세 기준. 코인은 표시가 장부 curr_p 우선이라
            # 여기까지 넘기면 신고가여도 갱신·로그가 안 될 수 있음.
            px_for_max = (
                float(run_bot.resolve_display_current_price(m, ledger_key, buy_p, current_p_api))
                if m == "COIN"
                else float(current_price)
            )
            if px_for_max > 0 and self.dashboard:
                self.dashboard.update_max_price_if_higher(ledger_key, px_for_max)

            if buy_p > 0: roi = ((current_price - buy_p) / buy_p) * 100
        except Exception as e:
            print(f"현재가 조회 에러 ({ticker_code}): {e}")

        return {
            "market": market,
            "name": name,
            "qty": qty,
            "buy_p_float": buy_p,
            "current_price": current_price,
            "roi": roi,
            "strategy": _dashboard_strategy_short(pos),
            "m_code": market_code,
            "t_code": ledger_key or str(ticker_code).strip().upper(),
            "pos_info": pos,
        }


class _GuiBackgroundSignals(QObject):
    """워커 스레드 → Qt 메인 스레드로만 시그널 전달 (UI 직접 조작 금지)."""

    net_check_result = pyqtSignal(bool)
    heartbeat_done = pyqtSignal()
    heartbeat_failed = pyqtSignal(str)


class BotDashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.initUI()
        self._last_holdings_roi = {}

        self._capital_thread: CapitalAdjustThread | None = None
        self._bg_sig = _GuiBackgroundSignals(self)
        self._bg_sig.net_check_result.connect(self._on_net_check_result)
        self._bg_sig.heartbeat_done.connect(self._on_heartbeat_done)
        self._bg_sig.heartbeat_failed.connect(self._on_heartbeat_failed)
        self._net_check_inflight = False

        self._stdout_redirect = RedirectText()
        self._stdout_redirect.out_signal.connect(self.append_log, Qt.QueuedConnection)
        sys.stdout = self._stdout_redirect
        sys.stderr = self._stdout_redirect

        # run_bot이 import 시 자동 실행되던 스캐너를 GUI에서 안전하게 가동
        try:
            import run_bot as _run_bot
            if not getattr(_run_bot, "_scanner_started", False):
                _run_bot._scanner_started = True
                if hasattr(_run_bot, "start_scanner_scheduler"):
                    _run_bot.start_scanner_scheduler()
        except Exception as e:
            print(f"⚠️ 스캐너 스케줄러 시작 실패: {e}")
        
        # 매매: 기동 1초 후 1회 → 이후 KST :00 / :15 / :30 / :45 에 정렬 (헤드리스 run_bot과 동일 리듬)
        
        # 3초마다 상단 성적표 UI 실시간 업데이트
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.update_stats_ui)
        self.ui_timer.start(3000)

        # 분봉 정렬 매매: static singleShot은 이전 예약을 취소하지 않아 중복 실행될 수 있음 → 단일 타이머로만 예약
        self._align_trade_timer = QTimer(self)
        self._align_trade_timer.setSingleShot(True)
        self._align_trade_timer.timeout.connect(self._aligned_trade_tick)
        self._trade_worker_busy = False
        self._heartbeat_halfhour_timer = QTimer(self)
        self._heartbeat_halfhour_timer.setSingleShot(True)
        self._heartbeat_halfhour_timer.timeout.connect(self._half_hour_heartbeat_due)
        self._heartbeat_pending_after_trade = False
        self._heartbeat_due_slot = ""
        self._refresh_inflight = False
        self._refresh_done_callbacks = []

        self._net_fail_count = 0
        self._net_watch_timer = QTimer(self)
        self._net_watch_timer.timeout.connect(self._check_network_and_maybe_restart)
        if os.environ.get("BOT_DISABLE_NET_WATCH", "").strip().lower() not in ("1", "true", "yes"):
            _nw_sec = (_NET_CHECK_INTERVAL_MS / 1000.0) * _NET_FAILS_BEFORE_EXIT
            print(
                f"  📡 [네트워크 감시] {_NET_CHECK_INTERVAL_MS / 1000:.0f}초마다 확인, "
                f"연속 {_NET_FAILS_BEFORE_EXIT}회 실패 시 종료(끊김 한도 약 {_nw_sec / 60:.1f}분). "
                f"끄기: 환경변수 BOT_DISABLE_NET_WATCH=1 · 간격/횟수: BOT_NET_WATCH_INTERVAL_MS, BOT_NET_WATCH_FAILS_BEFORE_EXIT"
            )
            self._net_watch_timer.start(_NET_CHECK_INTERVAL_MS)
        
        print("🤖 [시스템 가동] GUI 대시보드 초기화 중...")
        
        # 시작 시 1회 브로커/토큰 점검
        print("  🔌 브로커 객체 초기화 중...")
        try:
            refresh_brokers_if_needed(force=False)
            print("  ✅ 브로커 초기화 완료!")
        except Exception as e:
            print(f"  ⚠️ 브로커 초기화 중 오류: {e}")
            traceback.print_exc()
        
        # 시작 직후 화면은 1회 즉시 갱신(스냅샷 폴백 포함)하고, 매매는 정각 스케줄에서만 실행.
        QTimer.singleShot(150, lambda: self.refresh_balance(sync_first=False))

        # 시작 직후 매매는 실행하지 않고, 분봉 정각(:00/:15/:30/:45 KST)부터 반복
        self._schedule_next_aligned_trade()
        self._schedule_next_heartbeat_aligned()

    def _check_network_and_maybe_restart(self):
        """연속으로 외부망이 안 되면 프로세스 종료 -> run_bot.bat가 GUI 재실행. 검사는 백그라운드."""
        if self._net_check_inflight:
            return
        self._net_check_inflight = True

        def work():
            ok = _internet_reachable()
            self._bg_sig.net_check_result.emit(ok)

        threading.Thread(target=work, daemon=True).start()

    def _on_net_check_result(self, ok: bool):
        """메인 스레드: 네트워크 검사 결과만 반영."""
        try:
            if ok:
                self._net_fail_count = 0
                return
            self._net_fail_count += 1
            print(
                f"  ⚠️ [네트워크] 응답 없음 ({self._net_fail_count}/{_NET_FAILS_BEFORE_EXIT}, "
                f"간격 {_NET_CHECK_INTERVAL_MS/1000:.0f}s)"
            )
            if self._net_fail_count >= _NET_FAILS_BEFORE_EXIT:
                print("  🔄 [네트워크] 연속 실패 한도 초과 — GUI를 종료합니다. (run_bot.bat가 다시 띄우면 재연결)")
                QApplication.instance().quit()
        finally:
            self._net_check_inflight = False

    def _kst_halfhour_slot(self) -> str:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Asia/Seoul"))
        hh30 = 0 if now.minute < 30 else 30
        return now.strftime("%Y-%m-%d %H:") + f"{hh30:02d}"

    def _schedule_next_heartbeat_aligned(self):
        """다음 KST :00 또는 :30에 생존신고 예약(기동 시각과 무관)."""
        delay_ms = max(1, int(seconds_until_next_half_hour("Asia/Seoul") * 1000))
        self._heartbeat_halfhour_timer.stop()
        self._heartbeat_halfhour_timer.start(delay_ms)

    def _half_hour_heartbeat_due(self):
        """30분 슬롯 도래 — 해당 슬롯의 15분 매매 사이클 종료 후 전송하도록 표시."""
        self._heartbeat_due_slot = self._kst_halfhour_slot()
        self._heartbeat_pending_after_trade = True
        self._schedule_next_heartbeat_aligned()

    def _flush_pending_heartbeat(self):
        if not self._heartbeat_pending_after_trade:
            return
        if self._heartbeat_due_slot != self._kst_halfhour_slot():
            return
        self._heartbeat_pending_after_trade = False
        self._heartbeat_due_slot = ""
        self.send_heartbeat()

    def send_heartbeat(self):
        """run_bot heartbeat_report — 네트워크 지연 시에도 UI가 멈추지 않게 스레드에서 실행."""

        def work():
            try:
                heartbeat_report()
                self._bg_sig.heartbeat_done.emit()
            except Exception as e:
                self._bg_sig.heartbeat_failed.emit(str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_heartbeat_done(self):
        print("📲 텔레그램으로 예수금 및 시장 현황 보고서를 발송했습니다.")

    def _on_heartbeat_failed(self, msg: str):
        print(f"⚠️ 텔레그램 보고서 생성 또는 발송에 실패했습니다: {msg}")

    def initUI(self):
        self.setWindowTitle('3콤보 트레이딩 대시보드')
        self.resize(1480, 900)
        self.setMinimumSize(1280, 760)
        self.setStyleSheet(_dashboard_stylesheet())

        base_ui_font = QFont("Malgun Gothic", 10)
        self.setFont(base_ui_font)

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        
        self.stats_label = QLabel(get_current_stats(STATE_PATH), self)
        self.stats_label.setObjectName("StatsBanner")
        self.stats_label.setMinimumHeight(68)
        self.stats_label.setWordWrap(True)
        layout.addWidget(self.stats_label)
        
        top_layout = QHBoxLayout()
        top_layout.setSpacing(10)

        font_value = QFont("Malgun Gothic", 11, QFont.Bold)

        def make_market_box(object_name: str):
            box = QWidget()
            box.setObjectName(object_name)
            v = QVBoxLayout(box)
            v.setContentsMargins(10, 8, 10, 8)
            v.setSpacing(4)
            cash = QLabel("예수금: 조회중...")
            total = QLabel("총평가: 조회중...")
            roi = QLabel("보유수익률: 조회중...")
            for lbl in (cash, total, roi):
                lbl.setWordWrap(True)
                lbl.setFont(font_value)
                v.addWidget(lbl)
            return box, cash, total, roi

        kr_box, self.lbl_kr_cash, self.lbl_kr_total, self.lbl_kr_roi = make_market_box("MarketKr")
        us_box, self.lbl_us_cash, self.lbl_us_total, self.lbl_us_roi = make_market_box("MarketUs")
        coin_box, self.lbl_coin_cash, self.lbl_coin_total, self.lbl_coin_roi = make_market_box("MarketCoin")

        self.lbl_kr_cash.setText("🇰🇷 예수금: 조회중...")
        self.lbl_kr_total.setText("🇰🇷 총평가: 조회중...")
        self.lbl_kr_roi.setText("🇰🇷 보유수익률: 조회중...")

        self.lbl_us_cash.setText("🇺🇸 예수금: 조회중...")
        self.lbl_us_total.setText("🇺🇸 총평가: 조회중...")
        self.lbl_us_roi.setText("🇺🇸 보유수익률: 조회중...")

        self.lbl_coin_cash.setText("🪙 예수금: 조회중...")
        self.lbl_coin_total.setText("🪙 총평가: 조회중...")
        self.lbl_coin_roi.setText("🪙 보유수익률: 조회중...")

        top_layout.addWidget(kr_box)
        top_layout.addWidget(us_box)
        top_layout.addWidget(coin_box)
            
        btn_refresh = QPushButton("🔄 예수금 새로고침")
        btn_refresh.setObjectName("BtnRefresh")
        btn_refresh.setMinimumHeight(38)
        btn_refresh.clicked.connect(self.refresh_balance)
        top_layout.addWidget(btn_refresh)
        btn_force_refresh = QPushButton("🏦 KIS 강제 새로고침")
        btn_force_refresh.setObjectName("BtnForceKis")
        btn_force_refresh.setMinimumHeight(38)
        btn_force_refresh.clicked.connect(self.force_refresh_kis)
        top_layout.addWidget(btn_force_refresh)
        layout.addLayout(top_layout)
        
        settings_layout = QHBoxLayout()
        settings_layout.setSpacing(8)
        current_state = load_state(STATE_PATH)
        saved_settings = current_state.get("settings", {})
        
        settings_layout.addWidget(QLabel("🚀 최대 종목 수 ➔ 🇰🇷 국장:"))
        self.spin_max_kr = QSpinBox()
        self.spin_max_kr.setValue(saved_settings.get("max_pos_kr", 3))
        settings_layout.addWidget(self.spin_max_kr)
        
        settings_layout.addWidget(QLabel("🇺🇸 미장:"))
        self.spin_max_us = QSpinBox()
        self.spin_max_us.setValue(saved_settings.get("max_pos_us", 3))
        settings_layout.addWidget(self.spin_max_us)
        
        settings_layout.addWidget(QLabel("🪙 코인:"))
        self.spin_max_coin = QSpinBox()
        self.spin_max_coin.setValue(saved_settings.get("max_pos_coin", 5))
        settings_layout.addWidget(self.spin_max_coin)
        
        btn_apply_max = QPushButton("💾 설정 실시간 적용")
        btn_apply_max.setObjectName("BtnApplyMax")
        btn_apply_max.setMinimumHeight(34)
        btn_apply_max.clicked.connect(self._apply_max_position)
        settings_layout.addWidget(btn_apply_max)
        settings_layout.addStretch()
        
        layout.addLayout(settings_layout)

        # 탭 위젯 생성 (봇 브리핑 로그는 탭 밖 하단에 고정)
        tabs = QTabWidget()
        tabs.setTabBar(_DashboardTabBar())
        _configure_main_tab_widget(tabs)

        # 1. 실시간 현황 탭
        dashboard_tab = QWidget()
        dashboard_layout = QVBoxLayout(dashboard_tab)
        
        self.table = QTableWidget(0, 7)
        self.table.setAlternatingRowColors(True)
        self.table.setHorizontalHeaderLabels(
            [
                "시장",
                "종목명(코드)",
                "보유수량",
                "매수가",
                "현재가",
                "전략",
                "수량·수동매도",
            ]
        )
        _configure_holding_table(self.table)

        hold_title = QLabel("📊 현재 보유 종목 (국장 / 미장 / 코인)")
        hold_title.setObjectName("SectionTitle")
        dashboard_layout.addWidget(hold_title)
        dashboard_layout.addWidget(self.table)

        tabs.addTab(dashboard_tab, "실시간 현황")

        # 2. 매매 내역 탭
        history_tab = QWidget()
        history_layout = QVBoxLayout(history_tab)
        
        self.history_table = QTableWidget(0, 9)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.setHorizontalHeaderLabels(
            ["시간", "시장", "종목", "섹터", "구분", "수량", "가격", "수익률(%)", "사유"]
        )
        _configure_data_table(self.history_table, stretch_cols=(2, 8))
        self.history_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        history_layout.addWidget(self.history_table)
        
        tabs.addTab(history_tab, "매매 내역")

        # 3. 장부(포지션) 탭
        ledger_tab = QWidget()
        ledger_layout = QVBoxLayout(ledger_tab)
        
        self.ledger_table = QTableWidget(0, 9)
        self.ledger_table.setAlternatingRowColors(True)
        self.ledger_table.setHorizontalHeaderLabels(
            ["시장", "종목명(코드)", "매수가", "현재가", "최고가", "매도선", "수량", "매수·보유기간", "전략"]
        )
        _configure_data_table(self.ledger_table, stretch_cols=(1, 8))
        ledger_layout.addWidget(self.ledger_table)
        
        tabs.addTab(ledger_tab, "장부 (현재 포지션)")

        # 4. 매매·전략 안내 (타임스탑·매도선·러너 5MA)
        guide_tab = QWidget()
        guide_layout = QVBoxLayout(guide_tab)
        guide_title = QLabel("📘 매매·전략 안내 (V8 · SWING · Phase 1~5)")
        guide_title.setObjectName("SectionTitle")
        self.guide_console = QTextEdit()
        self.guide_console.setReadOnly(True)
        self.guide_console.setObjectName("LogConsole")
        self.guide_console.setText(_build_strategy_guide_text())
        guide_layout.addWidget(guide_title)
        guide_layout.addWidget(self.guide_console, 1)
        tabs.addTab(guide_tab, "매매·전략 안내")

        # 5. 합산 고점 보정 (adjust_capital.py와 동일 로직)
        capital_tab = QWidget()
        capital_layout = QVBoxLayout(capital_tab)
        capital_help = QLabel(
            "<b style='color:#e2e8f0'>고점 보정 (수동 입·출금)</b><br>"
            "<span style='color:#94a3b8'>예수금만 입·출금하면 Phase5 주차 고점과 실총액이 어긋날 수 있습니다. "
            "실행 시 실계좌 스냅샷 갱신 후 고점을 반영합니다.</span><br>"
            "<span style='color:#f87171'>주말 KIS 점검 구간에는 국·미 스냅샷이 제한될 수 있습니다.</span>"
        )
        capital_help.setWordWrap(True)
        capital_layout.addWidget(capital_help)

        kind_row = QHBoxLayout()
        self.capital_deposit_radio = QRadioButton("입금 (고점 +)")
        self.capital_withdraw_radio = QRadioButton("출금 (고점 −)")
        self.capital_deposit_radio.setChecked(True)
        self._capital_kind_group = QButtonGroup(self)
        self._capital_kind_group.addButton(self.capital_deposit_radio)
        self._capital_kind_group.addButton(self.capital_withdraw_radio)
        kind_row.addWidget(self.capital_deposit_radio)
        kind_row.addWidget(self.capital_withdraw_radio)
        kind_row.addStretch()
        capital_layout.addLayout(kind_row)

        amt_row = QHBoxLayout()
        amt_row.addWidget(QLabel("금액 (원):"))
        self.capital_amount_edit = QLineEdit()
        self.capital_amount_edit.setPlaceholderText("예: 1000000 또는 1,000,000")
        amt_row.addWidget(self.capital_amount_edit, stretch=1)
        capital_layout.addLayout(amt_row)

        self.capital_apply_btn = QPushButton("실행 (스냅샷 갱신 → 고점 반영)")
        self.capital_apply_btn.setObjectName("BtnCapitalApply")
        self.capital_apply_btn.setMinimumHeight(36)
        self.capital_apply_btn.clicked.connect(self._on_capital_adjust_clicked)
        capital_layout.addWidget(self.capital_apply_btn)
        capital_layout.addStretch()

        tabs.addTab(capital_tab, "고점 보정 (입출금)")

        log_header = QLabel("📝 실시간 작동 로그 (봇 브리핑)")
        log_header.setObjectName("SectionTitle")
        self.log_console = QTextEdit()
        self.log_console.setObjectName("LogConsole")
        self.log_console.setReadOnly(True)
        # 재연결·에러 폭주 시 메모리·페인트 폭주 완화 (구형 단말·공유기 재부팅 구간)
        self.log_console.document().setMaximumBlockCount(5000)
        self.log_console.setMinimumHeight(160)

        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(log_header)
        log_layout.addWidget(self.log_console, 1)

        main_split = QSplitter(Qt.Vertical)
        main_split.addWidget(tabs)
        main_split.addWidget(log_panel)
        main_split.setStretchFactor(0, 1)
        main_split.setStretchFactor(1, 0)
        main_split.setSizes([580, 240])
        layout.addWidget(main_split, 1)

    def _on_capital_adjust_clicked(self):
        import adjust_capital

        raw = self.capital_amount_edit.text().strip()
        try:
            amount = adjust_capital.parse_capital_amount_krw(raw)
        except ValueError as e:
            QMessageBox.warning(self, "입력 오류", str(e))
            return

        withdraw = self.capital_withdraw_radio.isChecked()
        verb = "출금" if withdraw else "입금"
        confirm = QMessageBox.question(
            self,
            "고점 보정 확인",
            f"<b>{verb}</b> <b>{amount:,.0f}</b>원을 합산 고점에 반영합니다.<br><br>"
            "실계좌 기준 <code>circuit_aux_*</code> 갱신 후 저장합니다. 계속할까요?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        self.capital_apply_btn.setEnabled(False)
        self._capital_thread = CapitalAdjustThread(withdraw, amount, STATE_PATH)
        self._capital_thread.done.connect(self._on_capital_adjust_finished)
        self._capital_thread.start()

    def _on_capital_adjust_finished(self, ok: bool, msg: str):
        self.capital_apply_btn.setEnabled(True)
        self._capital_thread = None
        if ok:
            QMessageBox.information(self, "고점 보정 완료", msg.replace("\n", "<br>"))
            self.refresh_balance()
        else:
            QMessageBox.warning(self, "고점 보정 실패", msg.replace("\n", "<br>"))

    def _qty_max_numeric(self, row: dict) -> float:
        """보유 행의 최대 매도 가능 수량(표시 qty와 동일 기준)."""
        try:
            return float(str(row.get("qty", "0")).replace(",", "").strip())
        except ValueError:
            return 0.0

    def _default_manual_sell_qty_text(self, row: dict) -> str:
        """수량 입력란 기본값 문자열 (보유 전량)."""
        is_coin = row.get("market") == "🪙 코인"
        qm = self._qty_max_numeric(row)
        if qm <= 0:
            return ""
        if is_coin:
            s = f"{qm:.8f}".rstrip("0").rstrip(".")
            return s if s else ""
        return str(int(round(qm)))

    def _on_manual_sell_click(self, row: dict, qty_edit: QLineEdit):
        """수량 입력 후 수동 매도 확인·주문 (빈 칸 = 보유 전량)."""
        market = row["m_code"]
        ticker = row["t_code"]
        name = row["name"]
        is_coin = row.get("market") == "🪙 코인"
        qty_max = self._qty_max_numeric(row)
        if qty_max <= 0:
            QMessageBox.warning(self, "매도 불가", "표시된 보유 수량이 없습니다.")
            return

        raw = (qty_edit.text() or "").strip().replace(",", "")
        if not raw:
            qty_use = qty_max
        else:
            try:
                qty_use = float(raw)
            except ValueError:
                QMessageBox.warning(self, "입력 오류", "매도 수량은 숫자로 입력해 주세요.")
                return

        tol = 1e-8 if is_coin else 1e-6
        if qty_use <= 0:
            QMessageBox.warning(self, "입력 오류", "매도 수량은 0보다 커야 합니다.")
            return
        if qty_use > qty_max + tol:
            QMessageBox.warning(self, "입력 오류", f"보유 수량을 초과했습니다. (최대 {qty_max:g})")
            return

        if not is_coin:
            if abs(qty_use - round(qty_use)) > 1e-6:
                QMessageBox.warning(self, "입력 오류", "국장·미장은 정수 주 단위만 매도할 수 있습니다.")
                return
            qty_final = int(round(qty_use))
        else:
            qty_final = float(qty_use)

        qty_label = str(qty_final)
        reply = QMessageBox.question(
            self,
            "매도 확인",
            f"<b>[{name} ({ticker})]</b><br><br>"
            f"매도 수량: <b>{qty_label}</b><br>"
            f"(표시 보유 최대 <b>{qty_max:g}</b>)<br><br>"
            "정말 매도하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if reply != QMessageBox.Yes:
            return

        unit = "" if is_coin else "주"
        print(f"▶️ [{name} ({ticker})] 사용자 수동 매도 요청 — 수량 {qty_label}{unit}")
        result = manual_sell(market, ticker, qty_final)

        if isinstance(result, dict):
            if result.get("success"):
                QMessageBox.information(
                    self,
                    "매도 성공",
                    result.get("message", "매도 주문이 성공적으로 전송되었습니다."),
                )
            else:
                QMessageBox.warning(
                    self,
                    "매도 실패",
                    result.get("message", "매도 주문에 실패했습니다."),
                )
        else:
            if bool(result):
                QMessageBox.information(self, "매도 성공", "매도 주문이 성공적으로 전송되었습니다.")
            else:
                QMessageBox.warning(self, "매도 실패", "매도 주문에 실패했습니다.")

        self.refresh_balance()

    def refresh_trade_history(self):
        """trade_history.json을 읽어 매매 내역 탭을 업데이트합니다."""
        from utils.trade_sector import sector_for_trade_record

        self.history_table.setRowCount(0)
        history = []
        history_changed = False
        sector_overlay = {}
        if TRADE_HISTORY_SECTOR_OVERLAY_PATH.exists():
            try:
                ov = json.loads(TRADE_HISTORY_SECTOR_OVERLAY_PATH.read_text(encoding="utf-8"))
                if isinstance(ov, dict):
                    sector_overlay = ov
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        if TRADE_HISTORY_PATH.exists():
            try:
                history = json.loads(TRADE_HISTORY_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        
        for item in history:
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            market = str(item.get("market", "") or "")
            ticker = str(item.get("ticker", "") or "")
            saved_name = str(item.get("name", "") or "").strip()
            display_name = ticker

            if ticker:
                symbol_name = saved_name
                if market in ("KR", "🇰🇷 국장") or ticker.isdigit():
                    if not symbol_name:
                        symbol_name = str(kr_name_dict.get(ticker, "") or "")
                    if not symbol_name:
                        try:
                            symbol_name = str(get_kr_company_name(ticker) or "")
                        except Exception:
                            symbol_name = ""
                elif market in ("US", "🇺🇸 미장"):
                    if not symbol_name:
                        symbol_name = str(us_name_dict.get(ticker, "") or "")
                    if not symbol_name:
                        try:
                            symbol_name = str(get_us_company_name(ticker) or "")
                        except Exception:
                            symbol_name = ""
                elif market in ("COIN", "🪙 코인"):
                    if not symbol_name and is_coin_ticker(ticker):
                        symbol_name = ticker.split("-", 1)[1]

                if symbol_name and symbol_name != ticker:
                    display_name = f"{symbol_name}({ticker})"

                # 기존 데이터 보정: name 필드가 비어 있으면 1회 저장
                if symbol_name and not saved_name:
                    item["name"] = symbol_name
                    history_changed = True

            sector_txt = sector_for_trade_record(item, sector_overlay) or "-"

            self.history_table.setItem(row, 0, _table_cell(item.get("timestamp", "")))
            self.history_table.setItem(row, 1, _table_cell(market))
            self.history_table.setItem(row, 2, _table_cell(display_name))
            self.history_table.setItem(row, 3, _table_cell(sector_txt))
            self.history_table.setItem(row, 4, _table_cell(item.get("side", "")))
            self.history_table.setItem(row, 5, _table_cell(str(item.get("qty", ""))))
            self.history_table.setItem(row, 6, _table_cell(str(item.get("price", ""))))
            self.history_table.setItem(row, 7, _table_cell(str(item.get("profit_rate", ""))))
            self.history_table.setItem(row, 8, _table_cell(item.get("reason", "")))

        if history_changed:
            try:
                TRADE_HISTORY_PATH.write_text(
                    json.dumps(history, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
                print("🛠️ 기존 매매내역 name 필드 자동 보정 완료")
            except Exception as e:
                print(f"⚠️ 매매내역 name 보정 저장 실패: {e}")

    def refresh_ledger(self):
        """bot_state.json의 positions를 읽어 장부(포지션) 탭을 업데이트합니다."""
        self.ledger_table.setRowCount(0)
        state = load_state(STATE_PATH)
        positions = state.get("positions", {})
        live_qty_by_key = _build_live_qty_lookup()

        for ticker, pos_info in positions.items():
            if not ticker or not isinstance(pos_info, dict):
                continue
            
            row = self.ledger_table.rowCount()
            self.ledger_table.insertRow(row)
            
            # 시장 판별
            market = ""
            if ticker.isdigit():
                market = "🇰🇷 국장"
                name = kr_name_dict.get(ticker, ticker)
                if not name or name == ticker:
                    try:
                        name = get_kr_company_name(ticker)
                    except:
                        name = ticker
            elif is_coin_ticker(ticker):
                market = "🪙 코인"
                name = ticker.replace("KRW-", "").replace("USDT-", "")
            else:
                market = "🇺🇸 미장"
                name = us_name_dict.get(ticker, ticker)
                if not name or name == ticker:
                    try:
                        name = get_us_company_name(ticker)
                    except:
                        name = ticker
            
            buy_p = float(pos_info.get('buy_p', 0.0))
            sl_p = float(pos_info.get('sl_p', 0.0))
            max_p = float(pos_info.get('max_p', 0.0))

            # 스윙: 장부 sl_p 대신 피보·구름·수익락 통합 매도선을 실시간 재계산(GUI 표시)
            st_led = str(pos_info.get("strategy_type") or "").strip().upper()
            tier_led = str(pos_info.get("tier") or "").strip().upper()
            if st_led == "SWING_FIB" or tier_led in ("SWING_FIB", "SWING"):
                try:
                    curr_led = 0.0
                    ohlcv_led = None
                    if market == "🪙 코인":
                        from api import coin_broker

                        curr_led = float(coin_broker.get_current_price(ticker) or 0.0)
                        ohlcv_led = coin_broker.fetch_ohlcv(ticker, "day", 250)
                    elif market == "🇰🇷 국장":
                        from strategy.rules import get_ohlcv_yfinance

                        ohlcv_led = get_ohlcv_yfinance(ticker)
                        if ohlcv_led and len(ohlcv_led) >= 2:
                            curr_led = float(ohlcv_led[-1].get("c", 0) or 0)
                    else:
                        from strategy.rules import get_ohlcv_yfinance

                        ohlcv_led = get_ohlcv_yfinance(ticker)
                        if ohlcv_led and len(ohlcv_led) >= 2:
                            curr_led = float(ohlcv_led[-1].get("c", 0) or 0)
                    if ohlcv_led and len(ohlcv_led) >= 60 and curr_led > 0:
                        mp_led = max(max_p if max_p > 0 else buy_p, curr_led)
                        pos_info = dict(pos_info)
                        pos_info["max_p"] = mp_led
                        reconcile_swing_position(
                            pos_info, ohlcv_led, reference_price=curr_led
                        )
                        m_led = (
                            "KR"
                            if market == "🇰🇷 국장"
                            else ("US" if market == "🇺🇸 미장" else "COIN")
                        )
                        sl_live = float(
                            get_swing_exit_display_price(
                                curr_led, pos_info, ohlcv_led, market=m_led, ticker=ticker
                            )
                        )
                        if sl_live > 0:
                            sl_p = sl_live
                except Exception:
                    pass
            
            strategy_label = _position_strategy_label(pos_info)
            buy_time = pos_info.get('buy_time', '')
            row_key = _ledger_row_key(ticker)
            qty_val = float(_to_float(live_qty_by_key.get(row_key), _to_float(pos_info.get("qty"), 0.0)))
            qty_text = _format_position_qty_for_table(market, qty_val)

            m_led = (
                "KR"
                if market == "🇰🇷 국장"
                else ("US" if market == "🇺🇸 미장" else "COIN")
            )
            curr_p = float(
                resolve_holding_display_price(m_led, ticker, buy_p, None, pos_info)
            )
            roi_led = (
                ((curr_p - buy_p) / buy_p) * 100.0 if buy_p > 0 and curr_p > 0 else 0.0
            )
            bundle = build_holding_display_bundle(
                m_led, ticker, name, buy_p, curr_p, pos_info, roi_pct=roi_led
            )

            self.ledger_table.setItem(row, 0, _table_cell(market))
            self.ledger_table.setItem(row, 1, _table_cell(f"{name} ({ticker})"))
            self.ledger_table.setItem(row, 2, _table_cell(bundle["buy_txt"]))
            self.ledger_table.setItem(row, 3, _table_cell(bundle["curr_txt"]))
            self.ledger_table.setItem(row, 4, _table_cell(bundle["max_txt"]))
            self.ledger_table.setItem(row, 5, _table_cell(bundle["sl_txt"]))
            self.ledger_table.setItem(row, 6, _table_cell(qty_text))
            # 매수 시각 + 보유시간 (타임스탑과 동일 — KR/US 영업·COIN 연속, N.Nh)
            buy_date_str = pos_info.get("buy_date", "")
            if buy_date_str:
                try:
                    buy_datetime_obj = datetime.fromisoformat(buy_date_str)
                    buy_time_display_str = buy_datetime_obj.strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    buy_time_display_str = buy_date_str[:16]
            else:
                try:
                    if isinstance(buy_time, (int, float)):
                        buy_time_display_str = datetime.fromtimestamp(buy_time).strftime(
                            "%Y-%m-%d %H:%M"
                        )
                    else:
                        buy_time_display_str = str(buy_time)[:16]
                except Exception:
                    buy_time_display_str = ""
            dur_led = _holding_duration_human(pos_info, m_led)
            if dur_led:
                buy_time_display_str = (
                    f"{buy_time_display_str}\n{dur_led}"
                    if buy_time_display_str
                    else dur_led
                )
            self.ledger_table.setItem(row, 7, _table_cell(buy_time_display_str))
            self.ledger_table.setItem(row, 8, _table_cell(strategy_label))

    def append_log(self, text):
        blob = (text or "").replace("\r", "").rstrip()
        if not blob:
            return
        try:
            from utils.yfinance_guard import is_yahoo_noise_line

            filtered = [
                ln for ln in blob.splitlines()
                if ln.strip() and not is_yahoo_noise_line(ln)
            ]
            if not filtered:
                return
            blob = "\n".join(filtered)
        except Exception:
            pass
        lg = get_quant_logger()
        if lg:
            try:
                for line in blob.splitlines():
                    if line.strip():
                        lg.info(line.rstrip())
            except Exception:
                pass
        raw = blob.strip()
        if not raw:
            return
        # run_bot 등이 이미 ``[HH:MM:SS]`` 를 붙인 줄이면 중복 접두어를 붙이지 않는다.
        if re.match(r"^\[\d{2}:\d{2}:\d{2}\]", raw):
            self.log_console.append(raw)
        else:
            time_str = datetime.now().strftime("%H:%M:%S")
            self.log_console.append(f"[{time_str}] {raw}")
        self.log_console.verticalScrollBar().setValue(self.log_console.verticalScrollBar().maximum())

    def update_stats_ui(self):
        self.stats_label.setText(get_current_stats(STATE_PATH, roi_info=self._last_holdings_roi))

    def _first_trade_then_schedule_aligned(self):
        """레거시 호환: 즉시 실행 없이 KST 분봉 정각 실행만 예약."""
        self._schedule_next_aligned_trade()

    def _schedule_next_aligned_trade(self):
        delay_ms = max(1, int(seconds_until_next_quarter_hour("Asia/Seoul") * 1000))
        self._align_trade_timer.stop()
        self._align_trade_timer.start(delay_ms)

    def _aligned_trade_tick(self):
        self.do_trade()
        self._schedule_next_aligned_trade()

    def _on_trade_worker_finished(self):
        self._trade_worker_busy = False
        self._flush_pending_heartbeat()
        # 매매 종료 후 최신 장부·내역을 화면에 즉시 반영
        QTimer.singleShot(500, lambda: self.refresh_balance(sync_first=False))

    def do_trade(self):
        if self._trade_worker_busy:
            print("  ⏭️ [시스템] 매매 엔진이 이미 실행 중입니다. 중복 호출을 건너뜁니다.")
            return
        self._trade_worker_busy = True
        try:
            print("\n▶️ [시스템] 봇 출동 전, 실시간 최고가(max_p) 사전 갱신 중...")
            # 매매는 갱신 완료 후 시작해 max_p 반영 정확도를 높입니다.
            def _launch_trade_worker():
                print("\n▶️ [시스템] 최신 최고가가 반영된 장부를 들고 사냥꾼 출동!")
                self.worker = WorkerThread()
                self.worker.finished.connect(self._on_trade_worker_finished)
                self.worker.start()
            self.refresh_balance(sync_first=False, on_finished=_launch_trade_worker)
        except Exception:
            self._trade_worker_busy = False
            raise

    def force_refresh_kis(self):
        """장외/쿨다운 중에도 KIS를 1회 강제 조회."""
        self.refresh_balance(sync_first=False, force_kis=True)

    def refresh_balance(self, sync_first=True, force_kis=False, on_finished=None):
        """비동기 스레드를 가동하여 화면 멈춤 없이 잔고를 갱신합니다."""
        if on_finished is not None and callable(on_finished):
            self._refresh_done_callbacks.append(on_finished)
        if self._refresh_inflight:
            return
        self._refresh_inflight = True
        print("🔄 예수금 및 보유종목 데이터를 갱신합니다... (GUI 백그라운드 처리)")
        
        # 일꾼 고용 및 데이터 받을 창구 연결
        self.updater_thread = BalanceUpdaterThread(sync_first=sync_first, dashboard=self, force_kis=force_kis)
        self.updater_thread.update_labels.connect(self._apply_labels_ui)
        self.updater_thread.update_table.connect(self._apply_table_ui)
        
        # 완료 시 장부 업데이트 등 마무리 작업 연결
        self.updater_thread.finished.connect(self._on_refresh_finished)
        self.updater_thread.error.connect(lambda msg: print(f"⚠️ 갱신 에러: {msg}"))
        
        # 일꾼 출동!
        self.updater_thread.start()

    def _apply_labels_ui(self, data):
        """스레드가 넘겨준 라벨 텍스트를 화면에 즉시 적용합니다."""
        def format_roi(roi): return f"{roi:+.2f}%" if roi is not None else "보유 없음"
        
        self.lbl_kr_cash.setText(f"🇰🇷 예수금: {data['kr']['cash']:,}원")
        self.lbl_kr_total.setText(f"🇰🇷 총평가: {data['kr']['total']:,}원")
        self.lbl_kr_roi.setText(f"🇰🇷 보유수익률: {format_roi(data['kr']['roi'])}")

        self.lbl_us_cash.setText(f"🇺🇸 예수금: ${data['us']['cash']:,.2f}")
        self.lbl_us_total.setText(f"🇺🇸 총평가: ${data['us']['total']:,.2f}")
        self.lbl_us_roi.setText(f"🇺🇸 보유수익률: {format_roi(data['us']['roi'])}")

        try:
            from api import coin_config as _cc_lbl

            _bn = _cc_lbl.is_binance()
        except Exception:
            _bn = False
        if _bn:
            from api import coin_broker as _cb_lbl

            _coin_fb = bool((data.get("coin") or {}).get("display_fallback"))
            _r = float(_cb_lbl.get_krw_per_usdt() or 0.0)
            if _coin_fb:
                self.lbl_coin_cash.setText(
                    f"🪙 예수금: {_gui_krw_to_usdt_label(int(data['coin']['cash']), _r)}"
                )
                self.lbl_coin_total.setText(
                    f"🪙 총평가: {_gui_krw_to_usdt_label(int(data['coin']['total']), _r)}"
                )
            else:
                try:
                    _cu, _tu = _cb_lbl.binance_display_cash_and_total_usdt()
                    self.lbl_coin_cash.setText(f"🪙 예수금: {_cu:,.2f} USDT")
                    self.lbl_coin_total.setText(f"🪙 총평가: {_tu:,.2f} USDT")
                except Exception:
                    self.lbl_coin_cash.setText(
                        f"🪙 예수금: {_gui_krw_to_usdt_label(int(data['coin']['cash']), _r)}"
                    )
                    self.lbl_coin_total.setText(
                        f"🪙 총평가: {_gui_krw_to_usdt_label(int(data['coin']['total']), _r)}"
                    )
        else:
            self.lbl_coin_cash.setText(f"🪙 예수금: {data['coin']['cash']:,}원")
            self.lbl_coin_total.setText(f"🪙 총평가: {data['coin']['total']:,}원")
        self.lbl_coin_roi.setText(f"🪙 보유수익률: {format_roi(data['coin']['roi'])}")

        self._last_holdings_roi = {k: v["roi"] for k, v in data.items() if v["roi"] is not None}

    def _apply_table_ui(self, rows_data):
        """스레드가 계산을 끝낸 테이블 데이터를 0.1초 만에 화면에 그립니다."""
        self.table.setRowCount(0)
        for d in rows_data:
            row = self.table.rowCount()
            self.table.insertRow(row)

            name_text, code_text = str(d['name']).strip(), str(d['t_code']).strip()
            display_name = (
                f"{name_text}({code_text})"
                if name_text and code_text and name_text != code_text
                else (code_text or name_text)
            )
            m_code = str(d.get("m_code") or _gui_market_code_from_label(d["market"]))
            pos_info = d.get("pos_info") if isinstance(d.get("pos_info"), dict) else {}
            bundle = build_holding_display_bundle(
                m_code,
                code_text,
                name_text,
                float(d["buy_p_float"]),
                float(d["current_price"]),
                pos_info,
                roi_pct=float(d["roi"]),
            )
            self.table.setItem(row, 0, _table_cell(d['market']))
            self.table.setItem(row, 1, _table_cell(display_name))
            self.table.setItem(
                row,
                2,
                _table_cell(_format_position_qty_for_table(d["market"], float(_to_float(d["qty"], 0.0)))),
            )
            qty_item = self.table.item(row, 2)
            if qty_item is not None:
                qty_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 3, _table_cell(bundle["buy_txt"]))
            self.table.setItem(row, 4, _table_cell(bundle["curr_txt"]))
            self.table.setItem(row, 5, _table_cell(str(d.get("strategy", "V8"))))

            cell = QWidget()
            row_lay = QHBoxLayout(cell)
            row_lay.setContentsMargins(4, 2, 4, 2)
            row_lay.setSpacing(6)
            qty_edit = QLineEdit()
            qty_edit.setPlaceholderText("비우면 전량")
            qty_edit.setText(self._default_manual_sell_qty_text(d))
            qty_edit.setMinimumWidth(80)
            qty_edit.setMaximumWidth(160)
            qty_edit.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            sell_btn = QPushButton("매도")
            sell_btn.setObjectName("BtnManualSell")
            sell_btn.setMinimumWidth(56)
            row_lay.addWidget(qty_edit)
            row_lay.addWidget(sell_btn)
            row_copy = dict(d)
            sell_btn.clicked.connect(
                lambda _checked=False, rp=row_copy, ed=qty_edit: self._on_manual_sell_click(rp, ed)
            )
            self.table.setCellWidget(row, 6, cell)

        _balance_holding_table_columns(self.table)

    def _on_refresh_finished(self):
        self._refresh_inflight = False
        self.refresh_trade_history()
        self.refresh_ledger()
        print("✅ 모든 계좌 정보 및 화면 표시가 최신 상태로 업데이트되었습니다.")
        callbacks = self._refresh_done_callbacks[:]
        self._refresh_done_callbacks.clear()
        for cb in callbacks:
            try:
                cb()
            except Exception as e:
                print(f"⚠️ 후속 작업 실행 오류: {e}")

    
    def update_max_price_if_higher(self, ticker, current_p):
        """
        GUI에서 확인된 현재가를 장부에 공유하고, 최고가보다 높으면 업데이트합니다.
        """
        try:
            if not STATE_PATH.exists():
                return

            state = load_state(STATE_PATH)
            positions = state.get("positions", {})

            ticker_key = str(ticker).strip().upper()
            # 최고가(max_p)는 정규장 중에만 갱신 (기존 정책 복원)
            market_type = "COIN" if is_coin_ticker(ticker_key) else ("KR" if ticker_key.isdigit() else "US")
            if not run_bot.is_market_open(market_type):
                return

            if ticker_key in positions:
                pos = positions[ticker_key]
                current_p_float = float(current_p)

                # 🔄 [핵심] 현재가를 장부에 상시 공유 (run_bot과 동기화용)
                pos["curr_p"] = current_p_float

                old_max = float(pos.get("max_p", 0.0))
                if current_p_float > old_max:
                    pos["max_p"] = current_p_float
                    msg = f"🚀 [GUI 감지] {ticker_key} 최고가 갱신! ({old_max:,.2f} ➔ {current_p_float:,.2f})"
                    print(msg)

                # 봇이 같은 구간에 저장했으면 병합 후 이 티커 가격만 다시 반영
                ledger_apply.merge_disk_if_newer(state, STATE_PATH)
                if ticker_key in state.get("positions", {}):
                    pos = state["positions"][ticker_key]
                    pos["curr_p"] = current_p_float
                    if current_p_float > float(pos.get("max_p", 0.0)):
                        pos["max_p"] = current_p_float
                save_state(STATE_PATH, state)

        except Exception as e:
            print(f"⚠️ 현재가 공유 오류 ({ticker}): {e}")
            
    def _apply_max_position(self):
        val_kr = self.spin_max_kr.value()
        val_us = self.spin_max_us.value()
        val_coin = self.spin_max_coin.value()
        
        try:
            # 1. 봇 안 끄고 메모리 실시간 덮어쓰기
            import run_bot
            run_bot.MAX_POSITIONS_KR = val_kr
            run_bot.MAX_POSITIONS_US = val_us
            run_bot.MAX_POSITIONS_COIN = val_coin
            
            # 2. 장부(bot_state.json) 영구 저장
            state = load_state(STATE_PATH)
            if "settings" not in state:
                state["settings"] = {}
            state["settings"]["max_pos_kr"] = val_kr
            state["settings"]["max_pos_us"] = val_us
            state["settings"]["max_pos_coin"] = val_coin

            save_state(STATE_PATH, state)
            
            msg = f"🔥 국장({val_kr}개), 미장({val_us}개), 코인({val_coin}개) 설정이 변경되었습니다."
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
            QMessageBox.information(self, "핫스왑 완료", msg + "\n(장부 저장 완료. 다음 매수 사이클부터 즉시 반영됩니다.)")
            
        except Exception as e:
            QMessageBox.critical(self, "오류", f"설정 저장 중 문제가 발생했습니다: {e}")

if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle("Fusion") 
    ex = BotDashboard()
    ex.show()
    sys.exit(app.exec_())