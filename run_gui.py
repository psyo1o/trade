# -*- coding: utf-8 -*-
"""
PyQt5 운영 GUI — ``run_bot`` 엔진을 탭·QTimer·스레드로 감싼다.

특징
    * 잔고·성적표·수동 매도·로그 뷰 등은 ``run_bot`` / ``execution`` / ``utils`` API를 그대로 호출.
    * ``import run_bot`` 시점에 ``config.json`` 이 로드되므로 **설정 변경 후 GUI 재시작** 필요.
    * **고점 보정 (입출금)** 탭: ``adjust_capital.py`` 와 동일하게 ``peak_total_equity``(및 ``peak_equity_total_krw`` 미러)·``capital_adjustments`` 반영 (백그라운드 스레드).
    * 매매는 기동 1초 후 1회 실행한 뒤, **KST :00 / :15 / :30 / :45**에 `run_trading_bot`을 맞춘다.
    * ``QTimer.singleShot`` 겹침으로 로그가 두 줄씩 나오는 것을 막기 위해 **단일 ``QTimer`` + 실행 중 가드**를 쓴다.
    * 네트워크 감시·생존신고(heartbeat)는 **백그라운드 스레드**에서 돌리고, 텔레는 **기동 직후 1회** 보낸 뒤 **KST :00 / :30** 벽시계에 맞춘다.

로그
    * ``RedirectText`` 가 ``utils.logger.get_quant_logger()`` 로 ``logs/bot.log`` 에도 한 줄씩 넘긴다.
"""
import warnings
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

import os
import sys, json
import socket
import threading
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLabel, QTextEdit, QTableWidget, QTableWidgetItem,
                             QHeaderView, QTabWidget, QMessageBox, QSpinBox, QLineEdit, QRadioButton,
                             QButtonGroup)
from PyQt5.QtCore import QTimer, pyqtSignal, QObject, QThread
from PyQt5.QtGui import QFont
from pathlib import Path
import traceback
import run_bot
from utils.logger import get_quant_logger
from utils.telegram import send_telegram
from utils.helpers import (
    get_kr_company_name,
    get_us_company_name,
    kis_equities_weekend_suppress_window_kst,
    seconds_until_next_quarter_hour,
    seconds_until_next_half_hour,
)
from execution.sync_positions import sync_all_positions
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
)
from execution.guard import load_state

TRADE_HISTORY_PATH = Path(__file__).resolve().parent / "trade_history.json"

# Internet watch: GUI exits after consecutive failures so run_bot.bat can relaunch.
_NET_CHECK_INTERVAL_MS = 12_000
_NET_FAILS_BEFORE_EXIT = 3
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
    out_signal = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        # ``setup_quant_logging()`` 은 ``utils.logger`` 에만 보관; ``run_bot.quant_logger`` 는 없음.
        self.logger = get_quant_logger()

    def write(self, text): 
        # 1. GUI 화면(실시간 로그 탭)으로 텍스트 발사!
        self.out_signal.emit(text)
        
        # 2. 파일 저장은 run_bot의 기관급 로거한테 토스! (매일 자정 롤오버 자동 처리)
        if self.logger and text.strip():
            for line in text.rstrip().splitlines():
                if line.strip():
                    self.logger.info(line.rstrip())

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
                    sync_all_positions(state, run_bot.get_held_stocks_kr(), run_bot.get_held_stocks_us(), run_bot.get_held_coins(), STATE_PATH)
                except Exception as e:
                    print(f"⚠️ 장부 동기화 중 오류: {e}")

            # 2~4. KR/US/COIN 스냅샷 공용 fetch (GUI force/쿨다운/백오프 정책 주입)
            if kis_equities_weekend_suppress_window_kst() and not self.sync_first:
                print("💤 [주말 점검] 증권사 API 통신을 건너뛰고 기존 장부를 유지합니다.")
            snap = run_bot.build_account_snapshot_for_report(
                allow_kis_fetch=_allow_kis_fetch,
                with_backoff=_with_backoff,
                force_kis_labels=self.force_kis,
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

        try:
            m = "KR" if market_code == "KR" else ("US" if market_code == "US" else "COIN")
            cp_api = current_p_api
            if m == "US":
                cp_api = run_bot.normalize_us_current_p_api_for_display(
                    buy_p,
                    current_p_api,
                    is_weekend=bool(kis_equities_weekend_suppress_window_kst()),
                )
            current_price = run_bot.resolve_display_current_price(m, ticker_code, buy_p, cp_api)

            if current_price > 0 and self.dashboard:
                self.dashboard.update_max_price_if_higher(ticker_code, current_price)

            if buy_p > 0: roi = ((current_price - buy_p) / buy_p) * 100
        except Exception as e:
            print(f"현재가 조회 에러 ({ticker_code}): {e}")

        return {"market": market, "name": name, "qty": qty, "buy_p_float": buy_p, "current_price": current_price, "roi": roi, "m_code": market_code, "t_code": ticker_code}


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
        self._stdout_redirect.out_signal.connect(self.append_log)
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
        self._refresh_inflight = False
        self._refresh_done_callbacks = []

        self._net_fail_count = 0
        self._net_watch_timer = QTimer(self)
        self._net_watch_timer.timeout.connect(self._check_network_and_maybe_restart)
        if os.environ.get("BOT_DISABLE_NET_WATCH", "").strip().lower() not in ("1", "true", "yes"):
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
        
        # 시작 직후 중복 API를 줄이기 위해 즉시 잔고 갱신은 생략하고,
        # 1초 후 첫 매매 직전 갱신에서 화면/장부를 함께 맞춥니다.
        # heartbeat는 API 다발이라 메인 스레드에서 돌리지 않음 — 기동 직후 1회 + 이후 KST :00 / :30 정렬
        QTimer.singleShot(0, self.send_heartbeat)

        # 시작 1초 후 매매 1회 → 다음 분봉 정각(:00/:15/:30/:45 KST)부터 반복
        QTimer.singleShot(1000, self._first_trade_then_schedule_aligned)

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
            print(f"  ⚠️ [네트워크] 응답 없음 ({self._net_fail_count}/{_NET_FAILS_BEFORE_EXIT})")
            if self._net_fail_count >= _NET_FAILS_BEFORE_EXIT:
                print("  🔄 [네트워크] 연결 불안으로 GUI를 종료합니다. (run_bot.bat가 곧 다시 실행)")
                QApplication.instance().quit()
        finally:
            self._net_check_inflight = False

    def _schedule_next_heartbeat_aligned(self):
        """다음 KST :00 또는 :30에 ``send_heartbeat`` 예약 (기동 시각과 무관)."""
        delay_ms = int(max(1.0, seconds_until_next_half_hour("Asia/Seoul")) * 1000)
        QTimer.singleShot(delay_ms, self.send_heartbeat)

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
        self._schedule_next_heartbeat_aligned()

    def _on_heartbeat_failed(self, msg: str):
        print(f"⚠️ 텔레그램 보고서 생성 또는 발송에 실패했습니다: {msg}")
        self._schedule_next_heartbeat_aligned()

    def initUI(self):
        self.setWindowTitle('🚀 64비트 3콤보 트레이딩 대시보드 (완전체)')
        self.resize(1180, 940)
        self.setMinimumSize(1020, 760)

        base_ui_font = QFont("Malgun Gothic", 11)
        self.setFont(base_ui_font)

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        
        self.stats_label = QLabel(get_current_stats(STATE_PATH), self)
        self.stats_label.setMinimumHeight(72)
        self.stats_label.setWordWrap(True)
        self.stats_label.setStyleSheet("""
            font-size: 18px; font-weight: bold; color: #1A5276; 
            background-color: #EBF5FB; padding: 14px; 
            border-radius: 8px; border: 2px solid #3498DB;
        """)
        layout.addWidget(self.stats_label)
        
        top_layout = QHBoxLayout()

        font_value = QFont("Malgun Gothic", 12, QFont.Bold)

        def make_market_box():
            box = QWidget()
            v = QVBoxLayout(box)
            v.setContentsMargins(0, 0, 0, 0)
            cash = QLabel("예수금: 조회중...")
            total = QLabel("총평가: 조회중...")
            roi = QLabel("보유수익률: 조회중...")
            for lbl in (cash, total, roi):
                lbl.setWordWrap(True)
                lbl.setFont(font_value)
                v.addWidget(lbl)
            return box, cash, total, roi

        kr_box, self.lbl_kr_cash, self.lbl_kr_total, self.lbl_kr_roi = make_market_box()
        us_box, self.lbl_us_cash, self.lbl_us_total, self.lbl_us_roi = make_market_box()
        coin_box, self.lbl_coin_cash, self.lbl_coin_total, self.lbl_coin_roi = make_market_box()

        self.lbl_kr_cash.setText("🇰🇷 예수금: 조회중...")
        self.lbl_kr_total.setText("🇰🇷 총평가: 조회중...")
        self.lbl_kr_roi.setText("🇰🇷 보유수익률: 조회중...")

        self.lbl_us_cash.setText("🇺🇸 예수금: 조회중...")
        self.lbl_us_total.setText("🇺🇸 총평가: 조회중...")
        self.lbl_us_roi.setText("🇺🇸 보유수익률: 조회중...")

        self.lbl_coin_cash.setText("🪙 예수금(KRW): 조회중...")
        self.lbl_coin_total.setText("🪙 총평가: 조회중...")
        self.lbl_coin_roi.setText("🪙 보유수익률: 조회중...")

        top_layout.addWidget(kr_box)
        top_layout.addWidget(us_box)
        top_layout.addWidget(coin_box)
            
        btn_refresh = QPushButton("🔄 예수금 새로고침")
        btn_refresh.setMinimumHeight(44)
        btn_refresh.clicked.connect(self.refresh_balance)
        top_layout.addWidget(btn_refresh)
        btn_force_refresh = QPushButton("🏦 KIS 강제 새로고침")
        btn_force_refresh.setMinimumHeight(44)
        btn_force_refresh.setStyleSheet("background-color: #34495e; color: white; font-weight: bold;")
        btn_force_refresh.clicked.connect(self.force_refresh_kis)
        top_layout.addWidget(btn_force_refresh)
        layout.addLayout(top_layout)
        
        # 👇👇👇 [여기 빈 공간에 복붙해라] 👇👇👇
        settings_layout = QHBoxLayout()
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
        btn_apply_max.setStyleSheet("background-color: #2c3e50; color: white; font-weight: bold;")
        btn_apply_max.clicked.connect(self._apply_max_position)
        settings_layout.addWidget(btn_apply_max)
        settings_layout.addStretch()
        
        layout.addLayout(settings_layout)

        # 탭 위젯 생성
        tabs = QTabWidget()
        layout.addWidget(tabs)

        # 1. 실시간 현황 탭
        dashboard_tab = QWidget()
        dashboard_layout = QVBoxLayout(dashboard_tab)
        
        self.table = QTableWidget(0, 7) # 현재가, 수익률 컬럼 추가
        self.table.setHorizontalHeaderLabels(["시장", "종목명(코드)", "보유수량", "매수단가", "현재가", "수익률", "수동매도"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setColumnWidth(4, 80) # 매도 버튼 컬럼 폭 조절
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.horizontalHeader().setMinimumHeight(30)

        dashboard_layout.addWidget(QLabel("<b>📊 현재 보유 종목 (국장/미장/코인)</b>"))
        dashboard_layout.addWidget(self.table)
        
        dashboard_layout.addWidget(QLabel("<b>📝 실시간 작동 로그 (봇 브리핑)</b>"))
        self.log_console = QTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setStyleSheet(
            "background-color: #1e1e1e; color: #00ff00; font-family: Consolas, 'Malgun Gothic', monospace; font-size: 12px;"
        )
        self.log_console.setMinimumHeight(200)
        dashboard_layout.addWidget(self.log_console)

        tabs.addTab(dashboard_tab, "실시간 현황")

        # 2. 매매 내역 탭
        history_tab = QWidget()
        history_layout = QVBoxLayout(history_tab)
        
        self.history_table = QTableWidget(0, 8)
        self.history_table.setHorizontalHeaderLabels(["시간", "시장", "종목", "구분", "수량", "가격", "수익률(%)", "사유"])
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.history_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        self.history_table.horizontalHeader().setMinimumSectionSize(52)
        self.history_table.verticalHeader().setDefaultSectionSize(34)
        self.history_table.horizontalHeader().setMinimumHeight(30)
        history_layout.addWidget(self.history_table)
        
        tabs.addTab(history_tab, "매매 내역")

        # 3. 장부(포지션) 탭
        ledger_tab = QWidget()
        ledger_layout = QVBoxLayout(ledger_tab)
        
        self.ledger_table = QTableWidget(0, 9)
        self.ledger_table.setHorizontalHeaderLabels(["시장", "종목명(코드)", "매수가", "손절가", "수익률", "최고가", "수량", "매수시간", "전략"])
        self.ledger_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.ledger_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.ledger_table.horizontalHeader().setSectionResizeMode(8, QHeaderView.Stretch)
        self.ledger_table.horizontalHeader().setMinimumSectionSize(52)
        self.ledger_table.verticalHeader().setDefaultSectionSize(34)
        self.ledger_table.horizontalHeader().setMinimumHeight(30)
        ledger_layout.addWidget(self.ledger_table)
        
        tabs.addTab(ledger_tab, "장부 (현재 포지션)")

        # 4. 합산 고점 보정 (adjust_capital.py와 동일 로직)
        capital_tab = QWidget()
        capital_layout = QVBoxLayout(capital_tab)
        capital_help = QLabel(
            "<b>고점 보정 (수동 입·출금)</b><br>"
            "예수금만 입금/출금하면 Phase5 주차 고점(<code>peak_total_equity</code>, 미러 <code>peak_equity_total_krw</code>)과 실총액이 어긋날 수 있습니다.<br>"
            "실행 시 CLI와 같이 <b>실계좌 스냅샷 갱신</b> 후 고점을 가산/감산하고 <code>capital_adjustments</code>에 기록합니다.<br>"
            "<span style='color:#c0392b'>주말 KIS 점검 구간에는 국·미 스냅샷이 제한될 수 있습니다.</span>"
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
        self.capital_apply_btn.setMinimumHeight(40)
        self.capital_apply_btn.setStyleSheet("background-color: #1e8449; color: white; font-weight: bold;")
        self.capital_apply_btn.clicked.connect(self._on_capital_adjust_clicked)
        capital_layout.addWidget(self.capital_apply_btn)
        capital_layout.addStretch()

        tabs.addTab(capital_tab, "고점 보정 (입출금)")

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

    def handle_manual_sell(self, market, ticker, qty, name):
        """수동 매도 버튼 클릭 시 호출되는 함수"""
        reply = QMessageBox.question(self, '매도 확인', f"<b>[{name} ({ticker})]</b><br><br>수량: {qty}주<br><br>정말로 매도하시겠습니까?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        
        if reply == QMessageBox.Yes:
            print(f"▶️ [{name} ({ticker})] 사용자가 수동 매도를 요청했습니다...")
            result = manual_sell(market, ticker, qty)

            if isinstance(result, dict):
                if result.get("success"):
                    QMessageBox.information(self, "매도 성공", result.get("message", "매도 주문이 성공적으로 전송되었습니다."))
                else:
                    QMessageBox.warning(self, "매도 실패", result.get("message", "매도 주문에 실패했습니다."))
            else:
                if bool(result):
                    QMessageBox.information(self, "매도 성공", "매도 주문이 성공적으로 전송되었습니다.")
                else:
                    QMessageBox.warning(self, "매도 실패", "매도 주문에 실패했습니다.")
            
            self.refresh_balance()

    def refresh_trade_history(self):
        """trade_history.json을 읽어 매매 내역 탭을 업데이트합니다."""
        self.history_table.setRowCount(0)
        history = []
        history_changed = False
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
                    if not symbol_name and ticker.startswith("KRW-"):
                        symbol_name = ticker.split("-", 1)[1]

                if symbol_name and symbol_name != ticker:
                    display_name = f"{symbol_name}({ticker})"

                # 기존 데이터 보정: name 필드가 비어 있으면 1회 저장
                if symbol_name and not saved_name:
                    item["name"] = symbol_name
                    history_changed = True

            self.history_table.setItem(row, 0, QTableWidgetItem(item.get("timestamp", "")))
            self.history_table.setItem(row, 1, QTableWidgetItem(market))
            self.history_table.setItem(row, 2, QTableWidgetItem(display_name))
            self.history_table.setItem(row, 3, QTableWidgetItem(item.get("side", "")))
            self.history_table.setItem(row, 4, QTableWidgetItem(str(item.get("qty", ""))))
            self.history_table.setItem(row, 5, QTableWidgetItem(str(item.get("price", ""))))
            self.history_table.setItem(row, 6, QTableWidgetItem(str(item.get("profit_rate", ""))))
            self.history_table.setItem(row, 7, QTableWidgetItem(item.get("reason", "")))

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
            elif ticker.startswith("KRW-"):
                market = "🪙 코인"
                name = ticker.replace("KRW-", "")
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
            
            # 수익률 계산
            if buy_p > 0:
                profit_rate = ((max_p - buy_p) / buy_p) * 100
            else:
                profit_rate = 0.0
            
            tier = pos_info.get('tier', '')
            buy_time = pos_info.get('buy_time', '')
            qty = pos_info.get('qty', 1)  # 수량은 positions에 없을 수 있음
            
            # 테이블에 행 추가
            self.ledger_table.setItem(row, 0, QTableWidgetItem(market))
            self.ledger_table.setItem(row, 1, QTableWidgetItem(f"{name} ({ticker})"))
            
            # 가격 표시 (시장별 포맷)
            if market == "🇺🇸 미장":
                buy_p_str = f"${buy_p:.2f}"
                sl_p_str = f"${sl_p:.2f}"
                max_p_str = f"${max_p:.2f}"
            elif market == "🪙 코인":
                buy_p_str = f"{buy_p:,.4f}원" if buy_p < 100 else f"{int(buy_p):,}원"
                sl_p_str = f"{sl_p:,.4f}원" if sl_p < 100 else f"{int(sl_p):,}원"
                max_p_str = f"{max_p:,.4f}원" if max_p < 100 else f"{int(max_p):,}원"
            else: # 국장
                buy_p_str = f"{int(buy_p):,}원"
                sl_p_str = f"{int(sl_p):,}원"
                max_p_str = f"{int(max_p):,}원"
            
            self.ledger_table.setItem(row, 2, QTableWidgetItem(buy_p_str))
            self.ledger_table.setItem(row, 3, QTableWidgetItem(sl_p_str))
            self.ledger_table.setItem(row, 4, QTableWidgetItem(f"{profit_rate:+.2f}%"))
            self.ledger_table.setItem(row, 5, QTableWidgetItem(max_p_str))
            self.ledger_table.setItem(row, 6, QTableWidgetItem(str(qty)))
            
            # 매수시간 포맷 (buy_date 우선 사용)
            buy_date_str = pos_info.get('buy_date', '')
            if buy_date_str:
                try:
                    # ISO 형식 문자열을 파싱하여 포맷팅
                    buy_datetime_obj = datetime.fromisoformat(buy_date_str)
                    buy_time_display_str = buy_datetime_obj.strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    # 파싱 실패 시 원본 문자열 사용
                    buy_time_display_str = buy_date_str[:16] # "YYYY-MM-DDTHH:MM"에서 T 이후를 자름
            else:
                # buy_date가 없을 경우 buy_time (timestamp) 사용
                try:
                    if isinstance(buy_time, (int, float)):
                        buy_time_display_str = datetime.fromtimestamp(buy_time).strftime("%Y-%m-%d %H:%M")
                    else:
                        buy_time_display_str = str(buy_time)[:16]
                except:
                    buy_time_display_str = ""
            self.ledger_table.setItem(row, 7, QTableWidgetItem(buy_time_display_str))
            self.ledger_table.setItem(row, 8, QTableWidgetItem(tier or ""))

    def append_log(self, text):
        if text.strip():
            time_str = datetime.now().strftime("%H:%M:%S")
            self.log_console.append(f"[{time_str}] {text.strip()}")
            self.log_console.verticalScrollBar().setValue(self.log_console.verticalScrollBar().maximum())

    def update_stats_ui(self):
        self.stats_label.setText(get_current_stats(STATE_PATH, roi_info=self._last_holdings_roi))

    def _first_trade_then_schedule_aligned(self):
        """기동 직후 1회 매매 실행 후, KST 분봉 정각에 맞춰 다음 실행을 예약한다."""
        self.do_trade()
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

        self.lbl_coin_cash.setText(f"🪙 예수금(KRW): {data['coin']['cash']:,}원")
        self.lbl_coin_total.setText(f"🪙 총평가: {data['coin']['total']:,}원")
        self.lbl_coin_roi.setText(f"🪙 보유수익률: {format_roi(data['coin']['roi'])}")

        self._last_holdings_roi = {k: v["roi"] for k, v in data.items() if v["roi"] is not None}

    def _apply_table_ui(self, rows_data):
        """스레드가 계산을 끝낸 테이블 데이터를 0.1초 만에 화면에 그립니다."""
        self.table.setRowCount(0)
        for d in rows_data:
            row = self.table.rowCount()
            self.table.insertRow(row)
            
            # 종목명 포맷팅
            name_text, code_text = str(d['name']).strip(), str(d['t_code']).strip()
            display_name = f"{name_text}({code_text})" if name_text and code_text and name_text != code_text else (code_text or name_text)
            
            # 매수단가 포맷팅
            if d['market'] == "🇰🇷 국장": price_str = f"{int(d['buy_p_float']):,}원"
            elif d['market'] == "🇺🇸 미장": price_str = f"${d['buy_p_float']:,.2f}"
            else: price_str = f"{d['buy_p_float']:,.4f}원" if d['buy_p_float'] < 100 else f"{int(d['buy_p_float']):,}원"

            # 현재가 포맷팅
            if d['market'] == "🇰🇷 국장": curr_str = f"{int(d['current_price']):,}원"
            elif d['market'] == "🇺🇸 미장": curr_str = f"${d['current_price']:.2f}"
            else: curr_str = f"{d['current_price']:,.4f}원" if d['current_price'] < 100 else f"{int(d['current_price']):,}원"

            self.table.setItem(row, 0, QTableWidgetItem(d['market']))
            self.table.setItem(row, 1, QTableWidgetItem(display_name))
            self.table.setItem(row, 2, QTableWidgetItem(d['qty']))
            self.table.setItem(row, 3, QTableWidgetItem(price_str))
            self.table.setItem(row, 4, QTableWidgetItem(curr_str))
            self.table.setItem(row, 5, QTableWidgetItem(f"{d['roi']:+.2f}%"))

            # 매도 버튼 부착
            sell_btn = QPushButton("매도")
            sell_btn.clicked.connect(lambda _, m=d['m_code'], t=d['t_code'], q=d['qty'], n=d['name']: self.handle_manual_sell(m, t, q, n))
            self.table.setCellWidget(row, 6, sell_btn)

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
            state_file = STATE_PATH
            if not state_file.exists(): return
            
            import json
            state = json.loads(state_file.read_text(encoding="utf-8"))
            positions = state.get("positions", {})
            
            ticker_key = str(ticker).strip().upper()
            
            # 장 운영 시간 체크
            market_type = "COIN" if ticker_key.startswith("KRW-") else ("KR" if ticker_key.isdigit() else "US")
            if not run_bot.is_market_open(market_type): return

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
                
                # 실시간 가격 공유를 위해 무조건 저장
                state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

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
            
            import json
            STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
            
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