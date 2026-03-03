import warnings
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

import sys, json
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QLabel, QTextEdit, QTableWidget, QTableWidgetItem, 
                             QHeaderView, QTabWidget, QMessageBox)
from PyQt5.QtCore import QTimer, pyqtSignal, QObject, QThread
from PyQt5.QtGui import QFont
from pathlib import Path
import traceback
import main64
from main64 import (
    send_telegram,
    get_us_cash_real,
    get_held_stocks_us_detail,
    sync_all_positions,
    get_held_stocks_kr_info,
    get_held_stocks_us_info,
    get_held_stocks_coins_info,
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
from risk.guard import load_state

TRADE_HISTORY_PATH = Path(__file__).resolve().parent / "trade_history.json"

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
    def write(self, text): self.out_signal.emit(text)
    def flush(self): pass

class WorkerThread(QThread):
    def run(self):
        import main64
        try: main64.run_trading_bot()
        except Exception as e: print(f"⚠️ 백그라운드 에러 발생: {e}")

class BotDashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.initUI()
        self._last_holdings_roi = {}

        self._stdout_redirect = RedirectText()
        self._stdout_redirect.out_signal.connect(self.append_log)
        sys.stdout = self._stdout_redirect

        # main64가 import 시 자동 실행되던 스캐너를 GUI에서 안전하게 가동
        try:
            import main64 as _main64
            if not getattr(_main64, "_scanner_started", False):
                _main64._scanner_started = True
                if hasattr(_main64, "start_scanner_scheduler"):
                    _main64.start_scanner_scheduler()
        except Exception as e:
            print(f"⚠️ 스캐너 스케줄러 시작 실패: {e}")
        
        # 15분마다 매매 로직 실행
        self.trade_timer = QTimer(self)
        self.trade_timer.timeout.connect(self.do_trade)
        self.trade_timer.start(15 * 60 * 1000) 
        
        # 30분마다 텔레그램 생존신고
        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.timeout.connect(self.send_heartbeat)
        self.heartbeat_timer.start(30 * 60 * 1000) 
        
        # 3초마다 상단 성적표 UI 실시간 업데이트
        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.update_stats_ui)
        self.ui_timer.start(3000)
        
        print("🤖 [시스템 가동] GUI 대시보드 초기화 중...")
        
        # 🔧 브로커 초기화 (기존 토큰 재사용, force=False)
        print("  🔌 브로커 객체 초기화 중...")
        try:
            refresh_brokers_if_needed(force=False)
            print("  ✅ 브로커 초기화 완료!")
        except Exception as e:
            print(f"  ⚠️ 브로커 초기화 중 오류: {e}")
            traceback.print_exc()
        
        # 1초 뒤 매매 사이클에서 점검을 하므로, 시작 시점의 장부 점검은 생략합니다.
        self.refresh_balance(sync_first=False)
        self.send_heartbeat()

        # 15분 기다리지 않고 켜지자마자 1초 뒤 첫 스캔 시작!
        QTimer.singleShot(1000, self.do_trade)

    def send_heartbeat(self):
        """main64.py에 있는 통합 보고 함수를 호출하고, 성공 여부를 확인합니다."""
        try:
            heartbeat_report()
            print("📲 텔레그램으로 예수금 및 시장 현황 보고서를 발송했습니다.")
        except Exception as e:
            print(f"⚠️ 텔레그램 보고서 생성 또는 발송에 실패했습니다: {e}")

    def initUI(self):
        self.setWindowTitle('🚀 64비트 3콤보 트레이딩 대시보드 (완전체)')
        self.resize(950, 800)
        
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        
        self.stats_label = QLabel(get_current_stats(STATE_PATH), self)
        self.stats_label.setMinimumHeight(60)
        self.stats_label.setWordWrap(True)
        self.stats_label.setStyleSheet("""
            font-size: 16px; font-weight: bold; color: #1A5276; 
            background-color: #EBF5FB; padding: 12px; 
            border-radius: 8px; border: 2px solid #3498DB;
        """)
        layout.addWidget(self.stats_label)
        
        top_layout = QHBoxLayout()

        font_title = QFont("Arial", 12, QFont.Bold)
        font_value = QFont("Arial", 11, QFont.Bold)

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
        btn_refresh.setMinimumHeight(40)
        btn_refresh.clicked.connect(self.refresh_balance)
        top_layout.addWidget(btn_refresh)
        layout.addLayout(top_layout)

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

        dashboard_layout.addWidget(QLabel("<b>📊 현재 보유 종목 (국장/미장/코인)</b>"))
        dashboard_layout.addWidget(self.table)
        
        dashboard_layout.addWidget(QLabel("<b>📝 실시간 작동 로그 (봇 브리핑)</b>"))
        self.log_console = QTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setStyleSheet("background-color: #1e1e1e; color: #00ff00; font-family: Consolas;")
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
        history_layout.addWidget(self.history_table)
        
        tabs.addTab(history_tab, "매매 내역")

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
        if TRADE_HISTORY_PATH.exists():
            try:
                history = json.loads(TRADE_HISTORY_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        
        for item in history:
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            self.history_table.setItem(row, 0, QTableWidgetItem(item.get("timestamp", "")))
            self.history_table.setItem(row, 1, QTableWidgetItem(item.get("market", "")))
            self.history_table.setItem(row, 2, QTableWidgetItem(item.get("ticker", "")))
            self.history_table.setItem(row, 3, QTableWidgetItem(item.get("side", "")))
            self.history_table.setItem(row, 4, QTableWidgetItem(str(item.get("qty", ""))))
            self.history_table.setItem(row, 5, QTableWidgetItem(str(item.get("price", ""))))
            self.history_table.setItem(row, 6, QTableWidgetItem(str(item.get("profit_rate", ""))))
            self.history_table.setItem(row, 7, QTableWidgetItem(item.get("reason", "")))

    def append_log(self, text):
        if text.strip():
            time_str = datetime.now().strftime("%H:%M:%S")
            self.log_console.append(f"[{time_str}] {text.strip()}")
            self.log_console.verticalScrollBar().setValue(self.log_console.verticalScrollBar().maximum())

    def update_stats_ui(self):
        self.stats_label.setText(get_current_stats(STATE_PATH, roi_info=self._last_holdings_roi))

    def do_trade(self):
        print("\n▶️ [시스템] 백그라운드 사냥꾼을 출동시킵니다. (화면 멈춤 없음!)")
        self.worker = WorkerThread()
        # 작업 완료 시 '장부 점검'은 생략하고 화면 정보만 갱신하도록 변경
        self.worker.finished.connect(lambda: self.refresh_balance(sync_first=False))
        self.worker.start()

    def refresh_balance(self, sync_first=True):
        print("🔄 예수금 및 보유종목 데이터를 갱신합니다...")
        
        # sync_first가 True일 때만 장부 점검을 실행
        if sync_first:
            try:
                state = load_state(STATE_PATH)
                sync_all_positions(
                    state, 
                    main64.get_held_stocks_kr(),  # 코드 리스트
                    main64.get_held_stocks_us(),  # 코드 리스트
                    main64.get_held_coins()       # 코드 리스트
                )
            except Exception as e:
                print(f"⚠️ 장부 동기화 중 오류 발생 (API 통신 문제 가능성): {e}")

        try:
            # [핵심 수정] 일반 API 호출을 '자동 복구' 기능이 탑재된 함수로 교체합니다.
            kr_bal = get_balance_with_retry()
            if kr_bal is None: kr_bal = {} # 통신 완전 실패 시 빈 딕셔너리로 초기화
            
            # 국장 예수금 추출
            d2_kr = 0
            if 'output2' in kr_bal:
                out2 = kr_bal['output2']
                if isinstance(out2, list) and len(out2) > 0:
                    d2_kr = int(_to_float(out2[0].get('prvs_rcdl_excc_amt', 0)))
                elif isinstance(out2, dict):
                    d2_kr = int(_to_float(out2.get('prvs_rcdl_excc_amt', 0)))
            
            kr_metrics = _calc_kr_holdings_metrics(kr_bal)
            kr_hold_roi = kr_metrics.get("roi")

            kr_total = None
            try:
                out2 = kr_bal.get("output2", [])
                if isinstance(out2, list) and out2:
                    kr_total = int(_to_float(out2[0].get("tot_evlu_amt"), d2_kr))
                elif isinstance(out2, dict):
                    kr_total = int(_to_float(out2.get("tot_evlu_amt"), d2_kr))
            except Exception:
                kr_total = None
            if kr_total is None:
                kr_total = int(d2_kr + float(kr_metrics.get("current", 0.0)))
            
            us_cash = _safe_num(get_us_cash_real(main64.broker_us), 0.0)
            us_bal = {}
            try:
                # [핵심 수정] 일반 API 호출을 '자동 복구' 기능이 탑재된 함수로 교체합니다.
                us_bal = get_us_positions_with_retry() or {}
            except Exception:
                us_bal = {}
            us_metrics = _calc_us_holdings_metrics(us_bal)
            us_hold_roi = us_metrics.get("roi")

            if us_cash <= 0 and isinstance(us_bal, dict):
                out2 = us_bal.get("output2", [])
                if isinstance(out2, list) and out2:
                    us_cash = _safe_num(out2[0].get("frcr_dncl_amt_2", out2[0].get("frcr_buy_amt_smtl", 0)), 0.0)
                elif isinstance(out2, dict):
                    us_cash = _safe_num(out2.get("frcr_dncl_amt_2", out2.get("frcr_buy_amt_smtl", 0)), 0.0)

            us_stock_value = float(us_metrics.get("current", 0.0) or 0.0)
            us_total = us_cash + us_stock_value

            krw_cash = int(_safe_num(main64.upbit.get_balance("KRW"), 0.0))
            upbit_bals = main64.upbit.get_balances() or []
            if not isinstance(upbit_bals, list):
                upbit_bals = []
            coin_metrics = _calc_coin_holdings_metrics(upbit_bals)
            coin_hold_roi = coin_metrics.get("roi")

            coin_value = float(coin_metrics.get("current", 0.0) or 0.0)
            coin_total = int(krw_cash + coin_value)

            self._last_holdings_roi = {}
            if kr_hold_roi is not None:
                self._last_holdings_roi["KR"] = kr_hold_roi
            if us_hold_roi is not None:
                self._last_holdings_roi["US"] = us_hold_roi
            if coin_hold_roi is not None:
                self._last_holdings_roi["COIN"] = coin_hold_roi

            kr_roi_text = f"{kr_hold_roi:+.2f}%" if kr_hold_roi is not None else "보유 없음"
            us_roi_text = f"{us_hold_roi:+.2f}%" if us_hold_roi is not None else "보유 없음"
            coin_roi_text = f"{coin_hold_roi:+.2f}%" if coin_hold_roi is not None else "보유 없음"

            self.lbl_kr_cash.setText(f"🇰🇷 예수금: {d2_kr:,}원")
            self.lbl_kr_total.setText(f"🇰🇷 총평가: {kr_total:,}원")
            self.lbl_kr_roi.setText(f"🇰🇷 보유수익률: {kr_roi_text}")

            self.lbl_us_cash.setText(f"🇺🇸 예수금: ${us_cash:,.2f}")
            self.lbl_us_total.setText(f"🇺🇸 총평가: ${us_total:,.2f}")
            self.lbl_us_roi.setText(f"🇺🇸 보유수익률: {us_roi_text}")

            self.lbl_coin_cash.setText(f"🪙 예수금(KRW): {krw_cash:,}원")
            self.lbl_coin_total.setText(f"🪙 총평가: {coin_total:,}원")
            self.lbl_coin_roi.setText(f"🪙 보유수익률: {coin_roi_text}")
            
            self.table.setRowCount(0)
            
            if isinstance(kr_bal, dict) and 'output1' in kr_bal and isinstance(kr_bal['output1'], list):
                for item in kr_bal['output1']:
                    qty_num = int(_safe_num(item.get('hldg_qty', item.get('ccld_qty_smtl1', 0)), 0))
                    if qty_num > 0:
                        code = item.get('pdno', '')
                        # 회사명: 딕셔너리 먼저 시도, 없으면 API 호출
                        name = kr_name_dict.get(code)
                        if not name:
                            name = main64.get_kr_company_name(code)
                        if not name:
                            name = code
                        qty = str(qty_num)
                        price = item.get('pchs_avg_prc', item.get('pchs_avg_pric', '0'))
                        current_p = item.get('prpr', '0')  # 현재가
                        self.add_table_row("🇰🇷 국장", name, qty, price, "KR", code, current_p)
                        
            # 🇺🇸 미장 테이블 그리기 (main64의 통합 함수 사용)
            try:
                # 💡 이제 여기서 복잡하게 item.get('pdno') 같은 거 안 해도 돼!
                us_data = get_held_stocks_us_detail() # main64에서 요리된 데이터 가져오기
                
                for item in us_data:
                    code = item['code']
                    name = us_name_dict.get(code, code)
                    qty = item['qty']
                    avg_price = item['avg_p']
                    
                    # 화면에 뿌려주기
                    self.add_table_row(
                        "🇺🇸 미장", 
                        name, 
                        str(qty), 
                        f"${avg_price:,.2f}",
                        "US",
                        code
                    )
            except Exception as e:
                print(f"⚠️ 미장 잔고 GUI 에러: {e}")
            if upbit_bals:
                for coin in upbit_bals:
                    if coin.get('currency') != "KRW" and _safe_num(coin.get('balance'), 0.0) > 0.00000001:
                        code = f"KRW-{coin['currency']}"
                        qty = str(_safe_num(coin.get('balance'), 0.0))
                        avg_buy_price = _safe_num(coin.get('avg_buy_price', 0), 0.0)
                        price = f"{avg_buy_price:,.0f}"
                        self.add_table_row("🪙 코인", coin['currency'], qty, price, "COIN", code)

            self.refresh_trade_history() # 매매 내역 탭도 함께 갱신
            print("✅ 모든 계좌 정보 및 화면 표시가 최신 상태로 업데이트되었습니다.")

        except Exception as e:
            print(f"⚠️ 데이터 갱신 실패: {e}")
            traceback.print_exc()

    def add_table_row(self, market, name, qty, price, market_code, ticker_code, current_p=None):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(market))
        self.table.setItem(row, 1, QTableWidgetItem(str(name)))
        self.table.setItem(row, 2, QTableWidgetItem(str(qty)))
        
        # 매수단가 포맷팅 (국장: 원, 미장: 달러)
        if market == "🇰🇷 국장":
            price_str = f"{int(float(str(price).replace('$', '').replace(',', ''))):,}원"
        else:  # 미장
            price_str = f"${float(str(price).replace('$', '').replace(',', '')):,.2f}"
        
        self.table.setItem(row, 3, QTableWidgetItem(price_str))
        
        # 현재가와 수익률 계산
        current_price = 0.0
        roi = 0.0
        try:
            # price 정제 (달러 기호/쉼표 제거)
            price_str = str(price).replace('$', '').replace(',', '')
            buy_p = float(price_str)
            
            if market == "🇰🇷 국장":
                # KR: 전달받은 현재가 사용 (prpr)
                if current_p:
                    current_price = _safe_num(current_p, buy_p)
                else:
                    current_price = buy_p
            elif market == "🇺🇸 미장":
                # US: yfinance에서 현재가 조회 (안정적)
                import yfinance as yf
                try:
                    ticker_info = yf.Ticker(ticker_code)
                    current_price = ticker_info.info.get('currentPrice')
                    if not current_price:
                        hist = ticker_info.history(period='1d')
                        if not hist.empty:
                            current_price = float(hist['Close'].iloc[-1])
                except:
                    # yfinance 실패시 get_ohlcv_yfinance 사용
                    from strategy.rules import get_ohlcv_yfinance
                    ohlcv = get_ohlcv_yfinance(ticker_code)
                    if ohlcv and len(ohlcv) > 0:
                        current_price = float(ohlcv[-1]['c'])
            
            # 현재가가 구해지지 않으면 매수 단가로 설정
            if current_price <= 0:
                current_price = buy_p
            
            # 수익률 계산
            if buy_p > 0:
                roi = ((current_price - buy_p) / buy_p) * 100
        except Exception as e:
            print(f"⚠️ 현재가 조회 실패 ({market_code}/{ticker_code}): {e}")
            current_price = buy_p
            roi = 0.0
        
        # 현재가 표시 (포맷에 맞춰)
        if market == "🇺🇸 미장":
            current_price_str = f"${current_price:.2f}"
        else:
            current_price_str = f"{int(current_price):,}원"
        
        self.table.setItem(row, 4, QTableWidgetItem(current_price_str))
        self.table.setItem(row, 5, QTableWidgetItem(f"{roi:+.2f}%"))

        sell_button = QPushButton("매도")
        sell_button.clicked.connect(lambda _, m=market_code, t=ticker_code, q=qty, n=name: self.handle_manual_sell(m, t, q, n))
        self.table.setCellWidget(row, 6, sell_button)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle("Fusion") 
    ex = BotDashboard()
    ex.show()
    sys.exit(app.exec_())