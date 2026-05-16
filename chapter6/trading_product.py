# ==============================
# ! Author: 이시운 (주식코딩 강의)
# ! 돌고래 전략 기반: 신호등 기법 + 이평선 + 블랙선 익절 + 기계적 손절
# ! All rights reserved.
# ==============================
#
# ▣ 자동화된 돌고래 전략 요소
#   - 신호등 기법: 🟢(전일종가 위) / 🟡(전일시가~종가) / 🔴(전일시가 아래)
#   - 매수 조건: 🟢 구간 + MA 이평선 조건
#   - 익절: 블랙선(전일 고가) 도달 시
#   - 손절①: profitSellLowerSpinBox % 이하 (기본 -2%)
#   - 손절②: 🔴 빨간불 구간 진입 시 즉시 청산
#   - 손절③: 초록선(전일 종가) 이탈 시 청산
# ==============================

import os
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"
import math
import sys
import time
import datetime

from loguru import logger
from PyQt5.QtWidgets import QApplication, QTableWidgetItem, QHeaderView, QWidget, QVBoxLayout
from PyQt5.QtCore import Qt, QTimer, QTime, QDate
from PyQt5.QtGui import QColor
import pandas as pd

from common_api import data_save_path, KiwoomAPI
from base_program import AutoTrader
from utils import log_exceptions, format_number, PandasModel
from sector_analyzer import SectorAnalyzer
from stock_scorer import StockScorer
from buy_signal import BuySignalScanner, CRITERIA, CRITERIA_LABELS
from db_manager import DBManager, DB_PATH
from flow_monitor import FlowMonitor
from dashboard import DashboardWidget


def _fmt_money(val: float) -> str:
    """억원 단위 값을 +XXX억 형태로 포맷. 0이면 '—' 반환."""
    if val == 0:
        return "—"
    return f"{val:+,.0f}억"


def _fmt_money_abs(val: float) -> str:
    """억원 단위 값을 X.X조 또는 XXXX억 형태로 포맷."""
    if val >= 10000:
        return f"{val / 10000:.1f}조"
    return f"{val:,.0f}억"


class CustomAutoTrader(KiwoomAPI, AutoTrader):
    def __init__(self):
        super().__init__()

        # 숫자 포맷 입력
        self.customBuyAmountLineEdit.textChanged.connect(
            lambda: format_number(self.customBuyAmountLineEdit)
        )

        # 거래대금 상위 탭
        self.topTradingMarketComboBox.addItems(["전체(000)", "코스피(001)", "코스닥(101)"])
        self.topTradingFilterComboBox.addItems(["전체조회", "ETF+ETN 제외", "ETF 제외", "ETN 제외"])
        self.topTradingRefreshPushButton.clicked.connect(self._on_top_trading_refresh)
        self.topTradingAutoRefreshPushButton.clicked.connect(self._on_top_trading_auto_toggle)

        self._top_trading_timer = QTimer()
        self._top_trading_timer.timeout.connect(self._on_top_trading_refresh)

        # 종목별투자자 탭
        today   = QDate.currentDate()
        month_ago = today.addDays(-30)
        self.investorEndDateEdit.setDate(today)
        self.investorStartDateEdit.setDate(month_ago)
        self.investorStockCodeLineEdit.returnPressed.connect(self._on_investor_code_entered)
        self.investorRefreshPushButton.clicked.connect(self._on_investor_refresh)

        # 거래원 분석 탭
        self.brokerAnalysisDateEdit.setDate(QDate.currentDate())
        self.brokerStockCodeLineEdit.returnPressed.connect(self._on_broker_code_entered)
        self.brokerRefreshPushButton.clicked.connect(self._on_broker_refresh)
        self._broker_watching_code  = ""     # 현재 시간별 구독 중인 종목코드
        self._broker_rt_initialized = False  # 시간별 테이블 헤더 초기화 완료 여부
        self._broker_buy_order: list[tuple] = []   # [(n, name), ...]
        self._broker_sell_order: list[tuple] = []

        # 거래대금상위 → 거래원분석 자동 연동
        self.topTradingTableWidget.itemClicked.connect(self._on_top_trading_item_clicked)

        # 섹터정렬 토글
        self._sector_sort_mode = False
        self.topTradingSectorSortPushButton.clicked.connect(self._on_sector_sort_toggle)

        # 섹터 집계기
        self.sector_analyzer = SectorAnalyzer(on_update=self._on_sector_summary_updated)

        # 종목 강도 점수 집계기
        self.stock_scorer = StockScorer(on_update=self._on_score_updated)
        self._top_row_map:  dict[str, int] = {}   # code → 테이블 행 인덱스
        self._alert_state:  dict[str, int] = {}   # sector → 마지막 알림 레벨

        # 매수 신호 스캐너
        self.buy_signal_scanner = BuySignalScanner(on_signal=self._on_buy_signal_updated)
        self._buy_signal_prev_codes: set[str] = set()   # 이전 신호 종목 (신규 감지용)

        # 자동매매 종목 DataFrame 및 테이블 모델
        self.auto_trade_stock_df = self.load_auto_trader_df()
        self.auto_pd_model = PandasModel(self.auto_trade_stock_df)
        self.autoTradeTableView.setModel(self.auto_pd_model)
        self.autoTradeTableView.clicked.connect(self.on_auto_trade_table_view_clicked)

        # 현금/신용 잔고 테이블 모델
        self.account_pd_model = PandasModel(self.account_info_df)
        self.accountInfoTableView.setModel(self.account_pd_model)
        self.credit_pd_model = PandasModel(self.credit_account_info_df)
        self.creditAccountInfoTableView.setModel(self.credit_pd_model)

        # ── 돌고래 전략: 전일 OHLC 저장 (신호등 기법용)
        # {stock_code: {'블랙선': prev_high, '초록선': prev_close, '빨간선': prev_open}}
        self.prev_day_data: dict = {}

        # 타이머
        self.backup_timer        = QTimer()
        self.table_refresh_timer = QTimer()
        self.order_check_timer   = QTimer()

        # TR 큐 처리 타이머 (250ms)
        self.tr_timer = QTimer()
        self.tr_timer.timeout.connect(self.send_tr_request)
        self.tr_timer.start(250)

        # 주문 큐 처리 타이머
        self.order_timer = QTimer()
        self.order_timer.timeout.connect(self._process_orders_queue)
        self.order_timer.start(250)

        # 시장 당일 등락률 차트
        self._market_chart_data = {"KOSPI": [], "KOSDAQ": [], "선물": []}
        self._market_ax     = None
        self._market_fig    = None
        self._market_canvas = None
        try:
            self._setup_market_chart_tab()
            self._market_chart_timer = QTimer()
            self._market_chart_timer.timeout.connect(self._redraw_market_chart)
            self._market_chart_timer.start(3000)
        except Exception as e:
            logger.warning(f"[시장차트] 탭 초기화 실패 (matplotlib 없거나 Qt 충돌): {e}")

        # 테마별현황 탭
        self._setup_theme_tab()

        # 매수 신호 탭
        self._setup_buy_signal_tab()

        # 섹터 자금 흐름 히스토리 탭
        self._flow_history:         list  = []    # [{time, label, foreign:{}, inst:{}}]
        self._flow_last_snap_ts:    float = 0.0
        self._flow_snap_interval:   int   = 600   # 10분 기본 간격
        self._flow_display_mode:    str   = "외인"  # "외인"|"기관"|"합산"
        # [1051] 공식 컷오프 시각 → 라벨 (HH:MM → 표시 라벨)
        self._flow_schedule: dict[str, str] = {
            "09:19": "1차",  "09:51": "2차",
            "11:01": "3차",  "13:10": "4차",
            "14:18": "5차",  "15:20": "종가전",
        }
        self._flow_done_labels: set[str] = set()  # 오늘 이미 찍은 라벨
        self._setup_sector_flow_history_tab()
        # 1분마다 [1051] 스케줄 체크
        self._flow_schedule_timer = QTimer()
        self._flow_schedule_timer.timeout.connect(self._check_flow_schedule)
        self._flow_schedule_timer.start(60_000)

        # [1051] 장중투자자별매매 배치 조회
        self._intraday_inv_raw:    dict = {}   # {code: [{집계시간, 외국인, 기관계, ...}, ...]}
        self._intraday_inv_total:  int  = 0
        self._intraday_inv_done:   int  = 0
        self._intraday_inv_mode:   str  = "외인"   # "외인"|"기관계"|"합산"
        self._intraday_sector_agg: dict = {}       # {period: {sector: {"외인": val, "기관계": val}}}

        # 해외선물 탭
        self._ovs_code     = "NQM26"   # 기본 종목코드 (NQ Mini NASDAQ)
        self._ovs_candles:  list = []   # [(date, open, high, low, close, vol), ...]
        self._ovs_rt_data:  dict = {}   # 마지막 실시간 데이터
        self._setup_overseas_futures_tab()

        # 자금흐름 모니터 탭 (별도 창)
        self._flow_monitor_win: FlowMonitor | None = None
        self._setup_flow_monitor_tab()

        # 전략 대시보드 탭 (4패널 통합, 한 화면)
        self._dashboard = DashboardWidget()
        self._dashboard.set_trader(self)
        self.mainTabWidget.addTab(self._dashboard, "전략")

        # 섹터 자금 유입 패널 (화면 상단)
        self._setup_sector_flow_panel()

        # 거래대금상위 탭을 맨 왼쪽(0번)으로 이동 + 선택
        for _i in range(self.mainTabWidget.count()):
            if "거래대금" in self.mainTabWidget.tabText(_i):
                self.mainTabWidget.tabBar().moveTab(_i, 0)
                self.mainTabWidget.setCurrentIndex(0)
                break

        # 외인/기관 자동 배치 조회 구조
        self._investor_auto_queue:   list[str]       = []
        self._investor_last_update:  dict[str, float] = {}
        self._auto_investor_code:    str              = ""
        self._investor_auto_timer = QTimer()
        self._investor_auto_timer.setInterval(1300)   # 1.3초 간격 (TR 제한 준수)
        self._investor_auto_timer.timeout.connect(self._investor_auto_next)

        # ── DB 영속성 ──────────────────────────────────────────
        self.db = DBManager()
        self._nxt_update_done: bool = False    # 오늘 09:05 next_day 업데이트 완료 여부
        self._nxt_save_done:   bool = False    # 오늘 20:00 nxt 저장 완료 여부

        # 1분마다 수급흐름 스냅샷 저장
        self._supply_save_timer = QTimer()
        self._supply_save_timer.timeout.connect(self._save_supply_snapshot)
        self._supply_save_timer.start(60_000)

    # ========================
    # 거래대금 상위 조회
    # ========================

    def _on_top_trading_refresh(self):
        market_map  = {0: "000", 1: "001", 2: "101"}
        market_type = market_map.get(self.topTradingMarketComboBox.currentIndex(), "000")
        filter_mode = self.topTradingFilterComboBox.currentIndex()
        self.request_top_trading_value(market_type=market_type, filter_mode=filter_mode)

    # ========================
    # 거래원 분석
    # ========================

    def _on_broker_code_entered(self):
        code = self.brokerStockCodeLineEdit.text().strip()
        name = self.stock_code_to_stock_name_dict.get(code, "")
        self.brokerStockNameLabel.setText(name)

    def _on_broker_refresh(self):
        code = self.brokerStockCodeLineEdit.text().strip().replace('_AL', '')
        if not code:
            return
        self._on_broker_code_entered()

        # opt10070 호출 (시간별/일별 공통) → on_receive_broker_analysis_data에서 모드 분기
        self._broker_watching_code  = code
        self._broker_rt_initialized = False
        self._broker_buy_order  = []
        self._broker_sell_order = []
        self.brokerTableWidget.setRowCount(0)
        self.brokerTableWidget.setColumnCount(0)
        self.request_broker_analysis(stock_code=code)

    def _investor_auto_next(self) -> None:
        """외인/기관 자동 배치 조회 — 큐에서 종목 하나씩 opt10059 요청 (1.3초 간격)."""
        if not self._investor_auto_queue:
            self._investor_auto_timer.stop()
            return

        code = self._investor_auto_queue.pop(0)

        # 10분 이내 업데이트된 종목은 건너뜀
        if time.time() - self._investor_last_update.get(code, 0) < 600:
            return

        today = datetime.date.today().strftime("%Y%m%d")
        try:
            self._auto_investor_code = code
            self.request_investor_data(
                stock_code=code,
                start_date=today,
                end_date=today,
                amount_qty="1",   # 1=금액, 2=수량
                trade_type="0",
            )
            self._investor_last_update[code] = time.time()
        except Exception as e:
            logger.debug(f"[자동외인조회] {code}: {e}")

    def _on_top_trading_item_clicked(self, item):
        """거래대금상위 행 클릭 → 거래원분석·종목별투자자 종목코드 세팅 + 투자자 자동조회 (탭 이동 없음)"""
        row = item.row()
        name_item = self.topTradingTableWidget.item(row, 2)
        if name_item is None:
            return
        code = name_item.data(Qt.UserRole)
        if not code:
            return
        # 거래원분석: 코드만 세팅 (자동조회 없음)
        self.brokerStockCodeLineEdit.setText(code)
        self._on_broker_code_entered()
        # 종목별투자자: 코드 세팅 후 자동조회
        self.investorStockCodeLineEdit.setText(code)
        self._on_investor_code_entered()
        self._on_investor_refresh()

    # ========================
    # 종목별투자자 탭
    # ========================

    def _on_investor_code_entered(self):
        code = self.investorStockCodeLineEdit.text().strip()
        name = self.stock_code_to_stock_name_dict.get(code, "")
        self.investorStockNameLabel.setText(name)

    def _on_investor_refresh(self):
        code = self.investorStockCodeLineEdit.text().strip()  # _AL 유지 → 통합 데이터 조회
        if not code:
            return
        self._on_investor_code_entered()
        # 수동 조회도 _auto_investor_code 갱신 → on_receive_investor_data가 정확한 종목 추적
        self._auto_investor_code = code.replace("_AL", "").strip()
        start = self.investorStartDateEdit.date().toString("yyyyMMdd")
        end   = self.investorEndDateEdit.date().toString("yyyyMMdd")
        amount_qty = "1" if self.investorAmountRadioButton.isChecked() else "2"
        if self.investorBuyRadioButton.isChecked():
            trade_type = "1"
        elif self.investorSellRadioButton.isChecked():
            trade_type = "2"
        else:
            trade_type = "0"
        self.common_log(f"[종목별투자자] {code} 조회 요청 중...")
        self.request_investor_data(
            stock_code=code, start_date=start, end_date=end,
            amount_qty=amount_qty, trade_type=trade_type,
        )

    def on_receive_investor_data(self, df: pd.DataFrame):
        if df.empty:
            self.common_log("[종목별투자자] 수신 데이터 없음")
            return
        self.common_log(f"[종목별투자자] {len(df)}건 수신 완료")

        # ── 섹터/점수 집계기 갱신: 실제 조회된 종목코드(_auto_investor_code) 사용 ──
        # LineEdit 코드를 우선하면, 사용자가 다른 종목을 클릭해 둔 상태에서
        # 자동 배치 조회 응답이 오면 잘못된 종목에 데이터가 귀속되는 버그가 발생함.
        update_code = getattr(self, '_auto_investor_code', '').strip()
        if not update_code:
            # 수동 조회 시 _auto_investor_code가 없으면 LineEdit 사용
            update_code = self.investorStockCodeLineEdit.text().strip().replace("_AL", "")
        if update_code and not df.empty:
            today_row = df.iloc[0]   # 최신일자가 0번째
            try:
                foreign_net = int(today_row.get("외국인", 0) or 0)
                inst_net    = int(today_row.get("기관계", 0) or 0)
                self.sector_analyzer.update_investor(update_code, foreign_net, inst_net)
                self.stock_scorer.update_investor(update_code, foreign_net, inst_net)
                logger.debug(f"[투자자] {update_code} 외인={foreign_net:+,} 기관={inst_net:+,}")
            except (TypeError, ValueError):
                pass
        table = self.investorTableWidget
        INVESTOR_COLS = [
            "일자", "종가", "대비", "거래량",
            "개인", "외국인", "기관계", "금융투자", "보험", "투신",
            "기타금융", "은행", "연기금등", "사모펀드", "국가", "기타법인", "내외국인",
        ]
        table.setRowCount(len(df))

        for row_idx, row in df.iterrows():
            for col_idx, col in enumerate(INVESTOR_COLS):
                val = row.get(col, 0)

                if col == "일자":
                    v = str(val)
                    text = f"{v[:4]}-{v[4:6]}-{v[6:]}" if len(v) == 8 else v
                    align = Qt.AlignCenter
                    fg = None
                elif col == "종가":
                    text = f"{abs(int(val)):,}" if val else ""
                    align = Qt.AlignRight | Qt.AlignVCenter
                    대비_val = int(row.get("대비", 0))
                    fg = QColor(Qt.red) if 대비_val > 0 else (QColor(Qt.blue) if 대비_val < 0 else None)
                elif col == "대비":
                    text = f"{int(val):+,}" if val else "0"
                    align = Qt.AlignRight | Qt.AlignVCenter
                    fg = QColor(Qt.red) if int(val) > 0 else (QColor(Qt.blue) if int(val) < 0 else None)
                elif col == "거래량":
                    try:
                        v = abs(int(val))
                    except (TypeError, ValueError):
                        v = 0
                    text = f"{v:,}" if v else ""
                    align = Qt.AlignRight | Qt.AlignVCenter
                    fg = None
                else:
                    # 투자자별 순매수/매수/매도 수량
                    try:
                        n = int(val)
                    except (TypeError, ValueError):
                        n = 0
                    text = f"{n:,}" if n != 0 else ""
                    align = Qt.AlignRight | Qt.AlignVCenter
                    fg = QColor(180, 0, 0) if n > 0 else (QColor(0, 0, 180) if n < 0 else None)

                item = QTableWidgetItem(text)
                item.setTextAlignment(align)
                if fg:
                    item.setForeground(fg)
                table.setItem(row_idx, col_idx, item)

        table.resizeColumnsToContents()

    # ========================
    # 테마별현황 탭
    # ========================

    def _setup_theme_tab(self):
        from PyQt5.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QLabel,
            QPushButton, QSpinBox, QComboBox, QSplitter, QTableWidget,
        )
        tab_widget = QWidget()
        layout = QVBoxLayout(tab_widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # ── 컨트롤 바 ─────────────────────────────────────────
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("기준일:"))
        self.themeDaySpinBox = QSpinBox()
        self.themeDaySpinBox.setRange(1, 99)
        self.themeDaySpinBox.setValue(1)
        self.themeDaySpinBox.setSuffix("일전")
        self.themeDaySpinBox.setFixedWidth(70)
        ctrl.addWidget(self.themeDaySpinBox)

        ctrl.addWidget(QLabel("정렬:"))
        self.themeSortComboBox = QComboBox()
        self.themeSortComboBox.addItems(["상위 등락률", "하위 등락률", "상위 기간수익률", "하위 기간수익률"])
        ctrl.addWidget(self.themeSortComboBox)

        self.themeRefreshButton = QPushButton("조회")
        self.themeRefreshButton.setFixedWidth(60)
        self.themeRefreshButton.clicked.connect(self._on_theme_refresh)
        ctrl.addWidget(self.themeRefreshButton)
        ctrl.addStretch()

        self._exportSectorBtn = QPushButton("업종 내보내기(xlsx)")
        self._exportSectorBtn.setFixedWidth(140)
        self._exportSectorBtn.clicked.connect(self._export_sector_excel)
        ctrl.addWidget(self._exportSectorBtn)

        self._importSectorBtn = QPushButton("업종 가져오기(xlsx)")
        self._importSectorBtn.setFixedWidth(140)
        self._importSectorBtn.clicked.connect(self._import_sector_excel)
        ctrl.addWidget(self._importSectorBtn)

        self._updateKrxBtn = QPushButton("KRX 업종 업데이트")
        self._updateKrxBtn.setFixedWidth(130)
        self._updateKrxBtn.setToolTip("네이버 금융에서 업종 분류를 다시 수집하여 krx_sector.json 갱신")
        self._updateKrxBtn.clicked.connect(self._on_update_krx_sectors)
        ctrl.addWidget(self._updateKrxBtn)

        layout.addLayout(ctrl)

        # ── 테마 순위 테이블 (상단) ────────────────────────────
        self.themeTableWidget = QTableWidget()
        self.themeTableWidget.setEditTriggers(QTableWidget.NoEditTriggers)
        self.themeTableWidget.setSelectionBehavior(QTableWidget.SelectRows)
        self.themeTableWidget.setAlternatingRowColors(True)
        _theme_headers = ["테마명", "종목수", "등락률(%)", "상승", "하락", "기간수익률(%)", "주요종목"]
        self.themeTableWidget.setColumnCount(len(_theme_headers))
        self.themeTableWidget.setHorizontalHeaderLabels(_theme_headers)
        self.themeTableWidget.horizontalHeader().setStretchLastSection(True)
        self.themeTableWidget.itemClicked.connect(self._on_theme_item_clicked)

        # ── 구성종목 테이블 (하단) ─────────────────────────────
        self.themeStockLabel = QLabel("← 테마를 클릭하면 구성종목이 표시됩니다")
        self.themeStocksTableWidget = QTableWidget()
        self.themeStocksTableWidget.setEditTriggers(QTableWidget.NoEditTriggers)
        self.themeStocksTableWidget.setSelectionBehavior(QTableWidget.SelectRows)
        self.themeStocksTableWidget.setAlternatingRowColors(True)
        _stock_headers = ["종목코드", "종목명", "현재가", "전일대비", "등락률(%)", "거래량"]
        self.themeStocksTableWidget.setColumnCount(len(_stock_headers))
        self.themeStocksTableWidget.setHorizontalHeaderLabels(_stock_headers)

        # 상·하단 스플리터
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.themeTableWidget)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(2)
        bl.addWidget(self.themeStockLabel)
        bl.addWidget(self.themeStocksTableWidget)
        splitter.addWidget(bottom)
        splitter.setSizes([350, 250])
        layout.addWidget(splitter)

        self.mainTabWidget.addTab(tab_widget, "테마별현황")

    def _on_theme_refresh(self):
        sort_map = {0: 3, 1: 4, 2: 1, 3: 2}   # 콤보박스 인덱스 → API 등락수익구분 코드
        sort_mode = sort_map.get(self.themeSortComboBox.currentIndex(), 3)
        date_offset = self.themeDaySpinBox.value()
        self.request_theme_group(date_offset=date_offset, sort_mode=sort_mode)

    def _on_theme_item_clicked(self, item):
        row = item.row()
        name_item = self.themeTableWidget.item(row, 0)
        if name_item is None:
            return
        theme_name = name_item.text()
        if not theme_name:
            return
        self.themeStockLabel.setText(f"[{theme_name}] 구성종목")
        self.request_theme_stocks(theme_name)

    def on_receive_theme_data(self, df: pd.DataFrame):
        if df.empty:
            self.common_log("[테마] 수신 데이터 없음 (모의투자 서버는 테마 데이터 미지원)")
            return
        table = self.themeTableWidget
        table.setRowCount(len(df))
        for row_idx, row in df.iterrows():
            pct = row.get("등락률", 0.0)
            pct_color = QColor(Qt.red) if pct > 0 else (QColor(Qt.blue) if pct < 0 else None)

            data = [
                (row.get("테마명",    ""),    Qt.AlignLeft | Qt.AlignVCenter,  None),
                (f"{row.get('종목수', 0)}",   Qt.AlignCenter,                   None),
                (f"{pct:+.2f}%",              Qt.AlignRight | Qt.AlignVCenter,  pct_color),
                (f"{row.get('상승종목수', 0)}", Qt.AlignCenter,                  QColor(Qt.red)),
                (f"{row.get('하락종목수', 0)}", Qt.AlignCenter,                  QColor(Qt.blue)),
                (f"{row.get('기간수익률', 0.0):+.2f}%", Qt.AlignRight | Qt.AlignVCenter,
                 QColor(Qt.red) if row.get("기간수익률", 0) > 0 else QColor(Qt.blue)),
                (row.get("주요종목", ""),      Qt.AlignLeft | Qt.AlignVCenter,   None),
            ]
            for col_idx, (text, align, fg) in enumerate(data):
                it = QTableWidgetItem(str(text))
                it.setTextAlignment(align)
                if fg:
                    it.setForeground(fg)
                table.setItem(row_idx, col_idx, it)
        table.resizeColumnsToContents()

        # ── 1단계: 주요종목 이름 → 즉시 업종맵 갱신 (빠른 선반영) ──
        instant = 0
        for _, row in df.iterrows():
            theme = row.get("테마명", "")
            major_str = row.get("주요종목", "")
            if not theme or not major_str:
                continue
            for name in [n.strip() for n in major_str.split(",") if n.strip()]:
                code = self.stock_name_to_stock_code_dict.get(name, "")
                if code:
                    self.stock_code_to_sector[code] = theme
                    instant += 1
        if instant:
            self._on_top_trading_refresh()   # 주요종목만으로 1차 표시

        # ── 2단계: 전 테마 opt90002 순차 요청 → 구성종목 전체 업종맵 갱신 ──
        themes = [row.get("테마명", "") for _, row in df.iterrows() if row.get("테마명", "")]
        # 카운터 초기화 (큐보다 먼저 설정해야 조기 완료 버그 방지)
        self._theme_sector_total = len(themes)
        self._theme_sector_done = 0
        self._theme_sector_queue.clear()
        for theme in themes:
            self.tr_req_queue.put([self.request_theme_stocks_for_sector, theme])
        self.common_log(f"[테마] {len(df)}개 테마 수신 / 주요종목 {instant}개 즉시 반영 / 전체 구성종목 {len(themes)}개 테마 순차 로딩 중...")

    def on_receive_theme_stocks(self, df: pd.DataFrame):
        table = self.themeStocksTableWidget
        if df.empty:
            table.setRowCount(0)
            return
        table.setRowCount(len(df))
        for row_idx, row in df.iterrows():
            pct = row.get("등락률", 0.0)
            pct_color = QColor(Qt.red) if pct > 0 else (QColor(Qt.blue) if pct < 0 else None)
            data = [
                (row.get("종목코드", ""),         Qt.AlignCenter,                  None),
                (row.get("종목명",   ""),         Qt.AlignLeft | Qt.AlignVCenter,  None),
                (f"{row.get('현재가', 0):,}",     Qt.AlignRight | Qt.AlignVCenter, None),
                (f"{row.get('전일대비', 0):+,}",  Qt.AlignRight | Qt.AlignVCenter, pct_color),
                (f"{pct:+.2f}%",                  Qt.AlignRight | Qt.AlignVCenter, pct_color),
                (f"{row.get('거래량', 0):,}",     Qt.AlignRight | Qt.AlignVCenter, None),
            ]
            for col_idx, (text, align, fg) in enumerate(data):
                it = QTableWidgetItem(str(text))
                it.setTextAlignment(align)
                if fg:
                    it.setForeground(fg)
                table.setItem(row_idx, col_idx, it)
        table.resizeColumnsToContents()

    def on_theme_sector_map_updated(self):
        """모든 테마 구성종목 갱신 완료 → 거래대금상위 최종 재조회"""
        self.common_log("[업종맵] 전 테마 구성종목 로딩 완료 → 거래대금상위 업종 최종 반영")
        self._on_top_trading_refresh()

    # ========================
    # 업종 엑셀 내보내기 / 가져오기
    # ========================

    def _on_update_krx_sectors(self):
        """build_krx_sectors.py 실행 → krx_sector.json 갱신 → 업종맵 즉시 재로드"""
        from PyQt5.QtWidgets import QMessageBox
        import subprocess, sys

        script = os.path.join(os.path.dirname(__file__), "build_krx_sectors.py")
        if not os.path.exists(script):
            QMessageBox.critical(self, "오류", f"스크립트를 찾을 수 없습니다:\n{script}")
            return

        reply = QMessageBox.question(
            self, "KRX 업종 업데이트",
            "네이버 금융에서 전체 업종 데이터를 새로 수집합니다.\n"
            "약 30~60초 소요되며, 인터넷 연결이 필요합니다.\n\n진행하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        self._updateKrxBtn.setEnabled(False)
        self._updateKrxBtn.setText("수집 중...")
        self.common_log("[KRX] 업종 데이터 수집 시작 (약 30~60초)...")

        # 백그라운드 프로세스로 실행
        self._krx_proc = subprocess.Popen(
            [sys.executable, script],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        # 완료 감지 타이머
        self._krx_poll_timer = QTimer()
        self._krx_poll_timer.timeout.connect(self._check_krx_update_done)
        self._krx_poll_timer.start(1000)

    def _check_krx_update_done(self):
        """백그라운드 프로세스 완료 여부 폴링"""
        from PyQt5.QtWidgets import QMessageBox
        import json as _json

        ret = self._krx_proc.poll()
        if ret is None:
            return   # 아직 실행 중

        self._krx_poll_timer.stop()
        out = self._krx_proc.stdout.read()
        self._updateKrxBtn.setEnabled(True)
        self._updateKrxBtn.setText("KRX 업종 업데이트")

        if ret != 0:
            self.common_log(f"[KRX] 업데이트 실패 (exit={ret}): {out[-200:]}")
            QMessageBox.critical(self, "오류", f"수집 실패:\n{out[-300:]}")
            return

        # krx_sector.json 즉시 재로드
        json_path = os.path.join(os.path.dirname(__file__), "krx_sector.json")
        try:
            with open(json_path, encoding="utf-8") as f:
                data = _json.load(f)
            added = 0
            for sector, codes in data.items():
                if sector.startswith("_") or not isinstance(codes, list):
                    continue
                for code in codes:
                    code = code.strip()
                    if code and code not in self.stock_code_to_sector:
                        self.stock_code_to_sector[code] = sector
                        added += 1
            self._on_top_trading_refresh()
            self.common_log(f"[KRX] 업종 업데이트 완료 — 신규 {added}개 추가, 누적 {len(self.stock_code_to_sector)}개")
            QMessageBox.information(self, "완료",
                f"KRX 업종 업데이트 완료\n신규 {added}개 추가, 누적 {len(self.stock_code_to_sector)}개")
        except Exception as e:
            QMessageBox.critical(self, "오류", f"krx_sector.json 재로드 실패: {e}")

    def _sync_sector_from_theme_table(self) -> int:
        """themeTableWidget의 주요종목 컬럼을 읽어 stock_code_to_sector 즉시 갱신.
        새로 매핑된 종목 수를 반환."""
        added = 0
        tbl = self.themeTableWidget
        for r in range(tbl.rowCount()):
            theme_item = tbl.item(r, 0)   # 테마명
            major_item = tbl.item(r, 6)   # 주요종목
            if not theme_item or not major_item:
                continue
            theme = theme_item.text().strip()
            if not theme:
                continue
            for raw in major_item.text().split(","):
                name = raw.strip()
                if not name:
                    continue
                code = self.stock_name_to_stock_code_dict.get(name, "")
                if code and code not in self.stock_code_to_sector:
                    self.stock_code_to_sector[code] = theme
                    added += 1
        return added

    def _export_sector_excel(self):
        """KOSPI+KOSDAQ 전체 종목(ETF/ETN 제외)을 현재 업종과 함께 Excel로 내보내기"""
        from PyQt5.QtWidgets import QFileDialog, QMessageBox
        import pandas as pd

        code_to_name = self.stock_code_to_stock_name_dict
        if not code_to_name:
            QMessageBox.information(self, "내보내기",
                "종목 목록이 로딩되지 않았습니다.\n로그인 후 다시 시도하세요.")
            return

        # 테마별현황 테이블의 주요종목 데이터로 업종맵 보완
        synced = self._sync_sector_from_theme_table()
        if synced:
            self.common_log(f"[업종] 테마 주요종목 재동기화: {synced}개 신규 매핑")

        rows = []
        for code, name in code_to_name.items():
            # ETF · ETN · 채권 등 분류 불필요 종목 제외
            if self._is_etf(name) or self._is_etn(name):
                continue
            rows.append({
                "종목코드": code,
                "종목명":   name,
                "업종":     self.stock_code_to_sector.get(code, ""),
            })

        df = pd.DataFrame(rows, columns=["종목코드", "종목명", "업종"])
        # 업종 빈 종목 먼저 → 업종명 가나다 → 종목명 가나다
        df["_blank"] = df["업종"].apply(lambda x: 0 if not x else 1)
        df = (df.sort_values(["_blank", "업종", "종목명"])
                .drop(columns="_blank")
                .reset_index(drop=True))

        default_path = os.path.join(os.path.dirname(__file__), "sector_list.xlsx")
        path, _ = QFileDialog.getSaveFileName(
            self, "업종 목록 저장", default_path, "Excel 파일 (*.xlsx)"
        )
        if not path:
            return

        try:
            with pd.ExcelWriter(path, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="업종목록")
                ws = writer.sheets["업종목록"]
                ws.column_dimensions["A"].width = 12
                ws.column_dimensions["B"].width = 22
                ws.column_dimensions["C"].width = 30
            blank_cnt = int((df["업종"] == "").sum())
            classified = len(df) - blank_cnt
            self.common_log(
                f"[업종] {len(df)}개 종목 내보내기 완료 "
                f"(분류 {classified}개 / 미분류 {blank_cnt}개) → {path}"
            )
            QMessageBox.information(self, "내보내기 완료",
                f"종목 수 (ETF/ETN 제외): {len(df)}개\n"
                f"  - 업종 분류됨: {classified}개\n"
                f"  - 업종 없음 (채워야 할 것): {blank_cnt}개\n\n"
                "※ 테마별현황 데이터 로딩이 완료되기 전에 내보내면\n"
                "   미분류 종목이 많을 수 있습니다.\n\n"
                "C열(업종)을 채운 뒤 '업종 가져오기' 버튼으로 불러오세요.")
        except Exception as e:
            QMessageBox.critical(self, "오류", f"저장 실패: {e}")

    def _import_sector_excel(self):
        """사용자가 채운 Excel을 읽어 업종맵 즉시 반영 + sector_list.xlsx 덮어쓰기"""
        from PyQt5.QtWidgets import QFileDialog, QMessageBox
        import pandas as pd
        import shutil

        path, _ = QFileDialog.getOpenFileName(
            self, "업종 목록 불러오기", os.path.dirname(__file__), "Excel 파일 (*.xlsx *.xls)"
        )
        if not path:
            return

        try:
            df = pd.read_excel(path, dtype=str)
            df.columns = [c.strip() for c in df.columns]
            if "종목코드" not in df.columns or "업종" not in df.columns:
                QMessageBox.critical(self, "오류",
                    "Excel에 '종목코드'와 '업종' 열이 필요합니다.")
                return

            updated = 0
            for _, row in df.iterrows():
                code   = str(row.get("종목코드", "")).strip().zfill(6)
                sector = str(row.get("업종", "")).strip()
                if not code or not sector or sector in ("nan", ""):
                    continue
                self.stock_code_to_sector[code] = sector
                updated += 1

            if updated == 0:
                QMessageBox.information(self, "가져오기", "업종이 채워진 항목이 없습니다.")
                return

            # sector_list.xlsx 로 저장 (다음 실행 시 _load_sector_xlsx 가 자동 로드)
            dest = os.path.join(os.path.dirname(__file__), "sector_list.xlsx")
            if os.path.abspath(path) != os.path.abspath(dest):
                shutil.copy2(path, dest)

            self._on_top_trading_refresh()
            self.common_log(f"[업종] {updated}개 업종 반영 완료 → sector_list.xlsx 저장")
            QMessageBox.information(self, "가져오기 완료",
                f"{updated}개 종목 업종 반영 완료.\n"
                f"(sector_list.xlsx 저장 → 다음 실행 시 자동 적용)")

        except Exception as e:
            QMessageBox.critical(self, "오류", f"가져오기 실패: {e}")

    # ========================
    # 시장 당일 등락률 차트 탭
    # ========================

    def _setup_market_chart_tab(self):
        import matplotlib
        matplotlib.rcParams['font.family'] = 'Malgun Gothic'
        matplotlib.rcParams['axes.unicode_minus'] = False
        from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
        from matplotlib.figure import Figure

        tab_widget = QWidget()
        layout = QVBoxLayout(tab_widget)
        layout.setContentsMargins(4, 4, 4, 4)

        self._market_fig = Figure(figsize=(10, 5), facecolor='#1c1c1c')
        self._market_ax  = self._market_fig.add_subplot(111, facecolor='#1c1c1c')
        self._market_canvas = FigureCanvasQTAgg(self._market_fig)
        layout.addWidget(self._market_canvas)

        self.mainTabWidget.addTab(tab_widget, "시장등락률")
        self._redraw_market_chart()

    def on_receive_market_chart_data(self, name: str, time_str: str, pct_change: float):
        if name not in self._market_chart_data:
            return
        pts = self._market_chart_data[name]
        # 동일 시각 중복 제거 (같은 초는 덮어쓰기)
        if pts and pts[-1][0] == time_str:
            pts[-1] = (time_str, pct_change)
        else:
            pts.append((time_str, pct_change))
        # 최대 1500포인트 유지 (~6.5시간 × 초당 틱)
        if len(pts) > 1500:
            self._market_chart_data[name] = pts[-1500:]

    def _redraw_market_chart(self):
        if self._market_ax is None:
            return
        ax = self._market_ax
        ax.clear()
        ax.set_facecolor('#1c1c1c')

        config = [("KOSPI", "#ff4444"), ("KOSDAQ", "#ffcc00"), ("선물", "#4488ff")]
        has_data = False

        for name, color in config:
            pts = self._market_chart_data.get(name, [])
            if not pts:
                continue
            has_data = True
            try:
                times = [datetime.datetime.strptime(p[0], "%H%M%S") for p in pts]
                pcts  = [p[1] for p in pts]
                ax.plot(times, pcts, color=color, linewidth=1.2, label=name)
            except Exception:
                continue

        ax.axhline(0, color='#888888', linewidth=0.8, linestyle='--')
        ax.tick_params(colors='#cccccc', labelsize=7)
        for spine in ax.spines.values():
            spine.set_edgecolor('#444444')
        ax.set_ylabel("등락률 (%)", color='#cccccc', fontsize=9)
        ax.set_title("시장 당일 등락률", color='#cccccc', fontsize=10, pad=4)

        if has_data:
            import matplotlib.dates as mdates
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            ax.legend(fontsize=8, facecolor='#2a2a2a', edgecolor='#555555',
                      labelcolor='white', loc='upper left')

        self._market_fig.tight_layout()
        self._market_canvas.draw_idle()

    def _get_front_month_futures_code(self) -> str:
        import calendar
        today = datetime.datetime.today()
        candidates = [(today.year, m) for m in (3, 6, 9, 12)] + [(today.year + 1, 3)]
        for year, month in candidates:
            if year < today.year or (year == today.year and month < today.month):
                continue
            cal = calendar.monthcalendar(year, month)
            thursdays = [week[3] for week in cal if week[3] != 0]
            expiry = datetime.datetime(year, month, thursdays[1], 15, 20)
            if today <= expiry:
                return f"101G{str(year)[2:]}{month:02d}"
        return f"101G{str(today.year + 1)[2:]}03"

    def on_receive_broker_analysis_data(self, snapshot: dict):
        """
        opt10070 싱글데이터 수신:
          - 시간별 모드: 거래원 이름을 헤더로 세팅 후 실시간 구독 시작
          - 일별  모드: 오늘 누적 합계를 단일 행으로 표시
        """
        if not snapshot:
            return
        table = self.brokerTableWidget
        BUY_COLOR  = QColor(255, 50,  50,  60)
        SELL_COLOR = QColor(50,  50,  255, 60)

        # 거래원 이름 추출
        buy_brokers  = [(n, snapshot[f"매수거래원{n}"]) for n in range(1, 6) if snapshot.get(f"매수거래원{n}")]
        sell_brokers = [(n, snapshot[f"매도거래원{n}"]) for n in range(1, 6) if snapshot.get(f"매도거래원{n}")]

        if not buy_brokers and not sell_brokers:
            self.common_log("[거래원] 데이터 없음 — 로그에서 필드명 확인 필요")
            return

        # 공통 헤더 구성
        headers = ["시간"]
        for _, name in buy_brokers:
            headers.append(f"매수\n{name}")
        for _, name in sell_brokers:
            headers.append(f"매도\n{name}")

        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        table.setColumnWidth(0, 90)

        if self.brokerTimeRadioButton.isChecked():
            # ── 시간별: 헤더 초기화 후 실시간 구독 시작 ──
            self._broker_buy_order  = buy_brokers
            self._broker_sell_order = sell_brokers
            self._broker_rt_initialized = True
            table.setRowCount(0)
            self.subscribe_broker_realtime(self._broker_watching_code)
            self.common_log(f"[거래원] {self._broker_watching_code} 시간별 실시간 구독 시작")
        else:
            # ── 일별: 오늘 누적 합계 1행으로 표시 ──
            table.setRowCount(1)
            today_str = datetime.datetime.now().strftime("%Y-%m-%d")
            item = QTableWidgetItem(today_str)
            item.setTextAlignment(Qt.AlignCenter)
            table.setItem(0, 0, item)
            col = 1
            for n, _ in buy_brokers:
                val = snapshot.get(f"매수거래원수량{n}", 0)
                it = QTableWidgetItem(f"{val:,}" if val else "")
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if val:
                    it.setBackground(BUY_COLOR)
                    it.setForeground(QColor(180, 0, 0))
                table.setItem(0, col, it)
                col += 1
            for n, _ in sell_brokers:
                val = snapshot.get(f"매도거래원수량{n}", 0)
                it = QTableWidgetItem(f"{val:,}" if val else "")
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if val:
                    it.setBackground(SELL_COLOR)
                    it.setForeground(QColor(0, 0, 180))
                table.setItem(0, col, it)
                col += 1

    def on_receive_broker_realtime(self, stock_code: str, data: dict):
        """주식거래원 실시간 수신 → 시간별 테이블에 최신 행 삽입"""
        if stock_code != self._broker_watching_code:
            return
        table = self.brokerTableWidget
        BUY_COLOR  = QColor(255, 50,  50,  60)
        SELL_COLOR = QColor(50,  50,  255, 60)

        # 첫 데이터 도착 시 헤더 구성 (매수1~5 → 매도1~5 순)
        if not self._broker_rt_initialized:
            buy_cols, sell_cols = [], []
            for n in range(1, 6):
                name = data.get(f"매수거래원{n}", "")
                if name:
                    buy_cols.append((n, name))
            for n in range(1, 6):
                name = data.get(f"매도거래원{n}", "")
                if name:
                    sell_cols.append((n, name))
            self._broker_buy_order  = buy_cols
            self._broker_sell_order = sell_cols
            headers = ["시간"]
            for _, name in buy_cols:
                headers.append(f"매수\n{name}")
            for _, name in sell_cols:
                headers.append(f"매도\n{name}")
            table.setColumnCount(len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
            table.setColumnWidth(0, 90)
            self._broker_rt_initialized = True

        # 맨 위에 새 행 삽입 (최신 데이터가 상단)
        table.insertRow(0)
        item = QTableWidgetItem(data["시간"])
        item.setTextAlignment(Qt.AlignCenter)
        table.setItem(0, 0, item)

        col = 1
        for n, _ in self._broker_buy_order:
            val = data.get(f"매수수량{n}", 0)
            item = QTableWidgetItem(f"{val:,}" if val else "")
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if val:
                item.setBackground(BUY_COLOR)
                item.setForeground(QColor(180, 0, 0))
            table.setItem(0, col, item)
            col += 1
        for n, _ in self._broker_sell_order:
            val = data.get(f"매도수량{n}", 0)
            item = QTableWidgetItem(f"{val:,}" if val else "")
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if val:
                item.setBackground(SELL_COLOR)
                item.setForeground(QColor(0, 0, 180))
            table.setItem(0, col, item)
            col += 1

        # 최대 500행 유지
        if table.rowCount() > 500:
            table.removeRow(table.rowCount() - 1)

    def on_tr_usage_updated(self, used: int, remaining: int):
        pct = used / 990 * 100
        color = "red" if remaining < 100 else ("orange" if remaining < 300 else "black")
        self.trUsageLabel.setText(
            f'<span style="color:{color}">TR 사용: {used} / 990 &nbsp;|&nbsp; 잔여: {remaining} ({100-pct:.0f}%)</span>'
        )

    # ========================
    # 실시간 섹터 자금 유입 패널
    # ========================

    def _setup_sector_flow_panel(self):
        from PyQt5.QtWidgets import (
            QFrame, QVBoxLayout, QHBoxLayout, QLabel,
            QPushButton, QTableWidget, QHeaderView,
            QSizePolicy, QListWidget,
        )

        _TBL_SS = (
            "QTableWidget { border: none; background: transparent; }"
            "QTableWidget::item { padding: 2px 6px; }"
            "QHeaderView::section { background: #f0f0f0; border: none;"
            "  border-bottom: 1px solid #ddd; padding: 2px 6px;"
            "  font-size: 11px; color: #555; }"
            "QTableWidget::item:selected { background: #e8f0ff; color: black; }"
        )

        panel = QFrame()
        panel.setFrameShape(QFrame.StyledPanel)
        panel.setObjectName("sectorFlowPanel")
        panel.setStyleSheet(
            "#sectorFlowPanel { border: 1px solid #d0d0d0;"
            "  border-radius: 4px; background: #fafafa; }"
        )
        panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        vl = QVBoxLayout(panel)
        vl.setContentsMargins(8, 4, 8, 4)
        vl.setSpacing(3)

        # ── 헤더 바 ──────────────────────────────────────────────
        hl = QHBoxLayout()
        title = QLabel("[ 핵심 섹터 모니터 ]")
        title.setStyleSheet("color: #999; font-size: 11px;")
        hl.addWidget(title)
        hl.addStretch()

        self._sector_status_label = QLabel("")
        self._sector_status_label.setStyleSheet("color: #888; font-size: 10px;")
        hl.addWidget(self._sector_status_label)

        copy_btn = QPushButton("⧉")
        copy_btn.setFixedSize(22, 20)
        copy_btn.setToolTip("클립보드에 복사")
        copy_btn.setStyleSheet("font-size: 12px; border: none;")
        copy_btn.clicked.connect(self._copy_sector_summary)
        hl.addWidget(copy_btn)
        vl.addLayout(hl)

        # ── 종목 강도 Top 3 바 ───────────────────────────────────
        self._score_top3_label = QLabel("강도점수 대기 중…")
        self._score_top3_label.setStyleSheet(
            "font-size: 11px; color: #888; padding: 1px 2px;"
        )
        self._score_top3_label.setTextFormat(Qt.RichText)
        vl.addWidget(self._score_top3_label)

        # ── 기존 섹터 요약 테이블 (9컬럼) ────────────────────────
        sector_tbl = QTableWidget()
        sector_tbl.setObjectName("sectorFlowTable")
        sector_tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        sector_tbl.setSelectionBehavior(QTableWidget.SelectRows)
        sector_tbl.setSelectionMode(QTableWidget.SingleSelection)
        sector_tbl.verticalHeader().setVisible(False)
        sector_tbl.setAlternatingRowColors(False)
        sector_tbl.setShowGrid(False)
        sector_tbl.setStyleSheet(_TBL_SS)
        sector_tbl.setColumnCount(9)
        sector_tbl.setHorizontalHeaderLabels([
            "섹터", "거래대금합", "평균등락률", "확산도",
            "5분속도", "10분속도", "외인순매수", "기관순매수", "대장주",
        ])
        hdr = sector_tbl.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(8, QHeaderView.Stretch)
        sector_tbl.verticalHeader().setDefaultSectionSize(22)
        sector_tbl.setRowCount(0)
        sector_tbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        sector_tbl.setFixedHeight(28)
        vl.addWidget(sector_tbl)
        self._sector_flow_table = sector_tbl

        # ── 구분선 ────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #e0e0e0;")
        vl.addWidget(sep)

        # ── 하단: TODAY LEADER(왼쪽) + 실시간 자금 유입(오른쪽) ──
        body_hl = QHBoxLayout()
        body_hl.setSpacing(8)

        # 왼쪽 — TODAY LEADER 테이블
        left_vl = QVBoxLayout()
        left_vl.setSpacing(1)
        lbl_leader = QLabel("[ TODAY LEADER ]")
        lbl_leader.setStyleSheet("color: #555; font-size: 10px; font-weight: bold;")
        left_vl.addWidget(lbl_leader)

        leader_tbl = QTableWidget()
        leader_tbl.setObjectName("leaderTable")
        leader_tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        leader_tbl.setSelectionBehavior(QTableWidget.SelectRows)
        leader_tbl.setSelectionMode(QTableWidget.SingleSelection)
        leader_tbl.verticalHeader().setVisible(False)
        leader_tbl.setAlternatingRowColors(False)
        leader_tbl.setShowGrid(False)
        leader_tbl.setStyleSheet(_TBL_SS)
        leader_tbl.setColumnCount(5)
        leader_tbl.setHorizontalHeaderLabels(["섹터", "대장주", "강도", "외인", "속도"])
        hdr2 = leader_tbl.horizontalHeader()
        hdr2.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr2.setSectionResizeMode(1, QHeaderView.Stretch)
        leader_tbl.verticalHeader().setDefaultSectionSize(22)
        leader_tbl.setRowCount(0)
        leader_tbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        leader_tbl.setFixedHeight(28)
        left_vl.addWidget(leader_tbl)
        body_hl.addLayout(left_vl, stretch=6)

        # 오른쪽 — 실시간 자금 유입 로그
        right_vl = QVBoxLayout()
        right_vl.setSpacing(1)
        lbl_flow = QLabel("[ 실시간 자금 유입 ]")
        lbl_flow.setStyleSheet("color: #555; font-size: 10px; font-weight: bold;")
        right_vl.addWidget(lbl_flow)

        log_widget = QListWidget()
        log_widget.setObjectName("flowLog")
        log_widget.setStyleSheet(
            "QListWidget { border: none; background: transparent; font-size: 11px; }"
            "QListWidget::item { padding: 1px 4px; }"
        )
        log_widget.setFixedWidth(240)
        log_widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        log_widget.setFixedHeight(28)
        right_vl.addWidget(log_widget)
        body_hl.addLayout(right_vl, stretch=3)

        vl.addLayout(body_hl)

        self._leader_table      = leader_tbl
        self._flow_log          = log_widget
        self._sector_flow_panel = panel

        # mainTabWidget 바로 앞에 삽입
        main_layout = self.centralWidget().layout()
        for i in range(main_layout.count()):
            item = main_layout.itemAt(i)
            if item and item.widget() is self.mainTabWidget:
                main_layout.insertWidget(i, panel)
                break

    # ── 속도 아이콘 ──────────────────────────────────────────────
    @staticmethod
    def _vel_icon(r5) -> str:
        try:
            v = float(r5)
        except (TypeError, ValueError):
            return "—"
        if pd.isna(v):  return "—"
        if v >= 100:    return "🔥🔥"
        if v >= 50:     return "🔥"
        if v >= 20:     return "📈"
        if v > 0:       return "→"
        return "▼"

    def _update_sector_flow_table(self, summary_df: pd.DataFrame) -> None:
        """섹터 요약 테이블(9컬럼) + TODAY LEADER + 자금 유입 알림 갱신."""
        if not hasattr(self, '_sector_flow_table'):
            return
        from PyQt5.QtWidgets import QTableWidgetItem

        display = summary_df[summary_df["종목수"] >= 1].head(8)

        # 속도 데이터
        vel_df = pd.DataFrame()
        try:
            vel_df = self.sector_analyzer.get_velocity_summary()
        except Exception:
            pass
        vel_map: dict[str, float] = {}
        if not vel_df.empty:
            for _, vr in vel_df.iterrows():
                vel_map[vr["섹터명"]] = float(vr["5분증가율(%)"])

        # leader_score 데이터
        leader_map: dict[str, tuple[str, float, str]] = {}
        try:
            ld_df = self.sector_analyzer.get_leader_scores()
            for _, lr in ld_df.iterrows():
                leader_map[lr["섹터명"]] = (lr["종목명"], lr["leader_score"], lr["등급"])
        except Exception:
            pass

        _GRADE_CLR = {
            "S": QColor(180, 0, 0), "A": QColor(200, 100, 0),
            "B": QColor(0, 80, 160), "C": QColor(120, 120, 120),
            "D": QColor(160, 160, 160),
        }
        ROW_H = 22
        n_rows = len(display)
        tbl_h  = max(28, n_rows * ROW_H + 26)

        # ── 기존 9컬럼 섹터 요약 테이블 ─────────────────────────
        stbl = self._sector_flow_table
        stbl.setRowCount(n_rows)
        stbl.setFixedHeight(tbl_h)

        for r, (_, row) in enumerate(display.iterrows()):
            sector = str(row["섹터명"])
            r5_val = vel_map.get(sector, float("nan"))
            try:
                r10_val = float(vel_df[vel_df["섹터명"] == sector]["10분증가율(%)"].values[0]) if not vel_df.empty else float("nan")
            except Exception:
                r10_val = float("nan")

            # 0 섹터명
            it = QTableWidgetItem(sector)
            it.setForeground(QColor("#1155cc"))
            it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            stbl.setItem(r, 0, it)

            # 1 거래대금합
            val = float(row["거래대금합(억)"])
            it = QTableWidgetItem(f"{val/10000:.1f}조" if val >= 10000 else f"{val:,.0f}억")
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            stbl.setItem(r, 1, it)

            # 2 평균등락률
            pct = float(row["평균등락률(%)"])
            it = QTableWidgetItem(f"{pct:+.1f}%")
            it.setForeground(QColor(Qt.red) if pct > 0 else (QColor(Qt.blue) if pct < 0 else QColor(Qt.black)))
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            stbl.setItem(r, 2, it)

            # 3 확산도
            diff = float(row.get("확산도(%)", 0))
            it = QTableWidgetItem(f"{diff:.0f}%")
            it.setForeground(QColor(Qt.red) if diff >= 70 else (QColor(200, 100, 0) if diff >= 50 else QColor(Qt.gray)))
            it.setTextAlignment(Qt.AlignCenter)
            stbl.setItem(r, 3, it)

            # 4 5분속도
            it = QTableWidgetItem(f"{r5_val:+.1f}%" if not pd.isna(r5_val) else "-")
            it.setTextAlignment(Qt.AlignCenter)
            if not pd.isna(r5_val):
                it.setForeground(QColor(180, 0, 0) if r5_val >= 50 else (QColor(200, 100, 0) if r5_val >= 20 else (QColor(Qt.darkGreen) if r5_val > 0 else QColor(Qt.gray))))
            stbl.setItem(r, 4, it)

            # 5 10분속도
            it = QTableWidgetItem(f"{r10_val:+.1f}%" if not pd.isna(r10_val) else "-")
            it.setTextAlignment(Qt.AlignCenter)
            if not pd.isna(r10_val):
                it.setForeground(QColor(180, 0, 0) if r10_val >= 50 else (QColor(200, 100, 0) if r10_val >= 20 else (QColor(Qt.darkGreen) if r10_val > 0 else QColor(Qt.gray))))
            stbl.setItem(r, 5, it)

            # 6 외인순매수
            fn = float(row["외인순매수합(주)"])
            it = QTableWidgetItem(_fmt_money(fn))
            it.setForeground(QColor(Qt.red) if fn > 0 else (QColor(Qt.blue) if fn < 0 else QColor("#aaa")))
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            stbl.setItem(r, 6, it)

            # 7 기관순매수
            inst = float(row["기관순매수합(주)"])
            it = QTableWidgetItem(_fmt_money(inst))
            it.setForeground(QColor(Qt.red) if inst > 0 else (QColor(Qt.blue) if inst < 0 else QColor("#aaa")))
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            stbl.setItem(r, 7, it)

            # 8 대장주 + leader_score
            ld_info = leader_map.get(sector)
            if ld_info:
                ld_name, ld_score, ld_grade = ld_info
                ld_text  = f"{ld_name}  {ld_score:.0f}점"
                ld_color = _GRADE_CLR.get(ld_grade, QColor("#cc4400"))
            else:
                ld_text  = str(row.get("대장주", ""))
                ld_color = QColor("#cc4400")
                ld_grade = ""
            it = QTableWidgetItem(ld_text)
            it.setForeground(ld_color)
            it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            if ld_grade == "S":
                f = it.font(); f.setBold(True); it.setFont(f)
            stbl.setItem(r, 8, it)

        # 상태 레이블
        total_val = float(summary_df["거래대금합(억)"].sum()) if not summary_df.empty else 0
        self._sector_status_label.setText(f"{n_rows}섹터 | 총 {_fmt_money_abs(total_val)}")

        # ── TODAY LEADER 테이블 ───────────────────────────────────
        if not hasattr(self, '_leader_table'):
            self._check_sector_alerts(display, vel_map, leader_map)
            return

        tbl = self._leader_table
        tbl.setRowCount(n_rows)
        tbl.setFixedHeight(tbl_h)
        if hasattr(self, '_flow_log'):
            self._flow_log.setFixedHeight(tbl_h)

        for r, (_, row) in enumerate(display.iterrows()):
            sector   = str(row["섹터명"])
            r5       = vel_map.get(sector, float("nan"))
            ld_info  = leader_map.get(sector)

            # 섹터
            it = QTableWidgetItem(sector)
            it.setForeground(QColor("#1155cc"))
            it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            tbl.setItem(r, 0, it)

            # 대장주 + leader_score
            if ld_info:
                ld_name, _, ld_grade = ld_info
                it = QTableWidgetItem(f"{ld_name}")
                it.setForeground(_GRADE_CLR.get(ld_grade, QColor("#cc4400")))
                if ld_grade == "S":
                    f = it.font(); f.setBold(True); it.setFont(f)
            else:
                it = QTableWidgetItem(str(row.get("대장주", "")))
                it.setForeground(QColor("#cc4400"))
            it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            tbl.setItem(r, 1, it)

            # 강도 (leader_score)
            score_txt = f"{ld_info[1]:.0f}" if ld_info else "—"
            score_clr = _GRADE_CLR.get(ld_info[2] if ld_info else "D", QColor(Qt.gray))
            it = QTableWidgetItem(score_txt)
            it.setForeground(score_clr)
            it.setTextAlignment(Qt.AlignCenter)
            if ld_info and ld_info[2] == "S":
                f = it.font(); f.setBold(True); it.setFont(f)
            tbl.setItem(r, 2, it)

            # 외인순매수
            fn = float(row.get("외인순매수합(주)", 0))
            it = QTableWidgetItem(_fmt_money(fn))
            it.setForeground(QColor(Qt.red) if fn > 0 else (QColor(Qt.blue) if fn < 0 else QColor("#aaa")))
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            tbl.setItem(r, 3, it)

            # 속도 아이콘
            icon = self._vel_icon(r5)
            it = QTableWidgetItem(icon)
            it.setTextAlignment(Qt.AlignCenter)
            if icon == "🔥🔥":
                it.setForeground(QColor(180, 0, 0))
            elif icon == "🔥":
                it.setForeground(QColor(200, 80, 0))
            elif icon == "📈":
                it.setForeground(QColor(Qt.darkGreen))
            else:
                it.setForeground(QColor(Qt.gray))
            tbl.setItem(r, 4, it)

        # 상태 레이블
        total_val = float(summary_df["거래대금합(억)"].sum()) if not summary_df.empty else 0
        self._sector_status_label.setText(
            f"{len(display)}섹터 | 총 {_fmt_money_abs(total_val)}"
        )

        # 자금 유입 알림 감지
        self._check_sector_alerts(display, vel_map, leader_map)

    def _check_sector_alerts(
        self,
        display: pd.DataFrame,
        vel_map: dict[str, float],
        leader_map: dict[str, tuple],
    ) -> None:
        """새로운 섹터 이벤트를 감지해 _flow_log에 추가."""
        # 알림 레벨: 1=강세, 2=확산, 3=급증, 4=폭발
        now_str = datetime.datetime.now().strftime("%H:%M")

        for _, row in display.iterrows():
            sector = str(row["섹터명"])
            r5     = vel_map.get(sector, 0.0)
            diff   = float(row.get("확산도(%)", 0))
            grade  = leader_map.get(sector, ("", 0, ""))[2] if leader_map.get(sector) else ""
            prev   = self._alert_state.get(sector, 0)

            if not pd.isna(r5) and r5 >= 100 and prev < 4:
                self._add_flow_alert(f"{now_str}  {sector}  거래대금 폭발", level=4)
                self._alert_state[sector] = 4
            elif not pd.isna(r5) and r5 >= 50 and prev < 3:
                self._add_flow_alert(f"{now_str}  {sector}  급증", level=3)
                self._alert_state[sector] = 3
            elif diff >= 70 and prev < 2:
                self._add_flow_alert(f"{now_str}  {sector}  확산 {diff:.0f}%", level=2)
                self._alert_state[sector] = 2
            elif grade == "S" and prev < 1:
                ld_name = leader_map[sector][0] if leader_map.get(sector) else ""
                self._add_flow_alert(f"{now_str}  {sector}  {ld_name} 강세", level=1)
                self._alert_state[sector] = 1

    def _add_flow_alert(self, text: str, level: int = 1) -> None:
        """_flow_log 상단에 이벤트 추가 (최대 50건 유지)."""
        if not hasattr(self, '_flow_log'):
            return
        from PyQt5.QtWidgets import QListWidgetItem
        _LEVEL_CLR = {
            4: QColor(180, 0, 0),
            3: QColor(200, 80, 0),
            2: QColor(0, 100, 160),
            1: QColor(80, 80, 80),
        }
        item = QListWidgetItem(text)
        item.setForeground(_LEVEL_CLR.get(level, QColor(Qt.black)))
        if level >= 3:
            f = item.font(); f.setBold(True); item.setFont(f)
        self._flow_log.insertItem(0, item)   # 최신이 위로
        while self._flow_log.count() > 50:
            self._flow_log.takeItem(self._flow_log.count() - 1)

    def _copy_sector_summary(self):
        """섹터 요약을 클립보드에 텍스트로 복사"""
        from PyQt5.QtWidgets import QApplication
        df = self.sector_analyzer.get_summary()
        if df.empty:
            return
        try:
            vel_df = self.sector_analyzer.get_velocity_summary()
            vel_map = {} if vel_df.empty else {
                row["섹터명"]: (row["5분증가율(%)"], row["10분증가율(%)"])
                for _, row in vel_df.iterrows()
            }
        except Exception:
            vel_map = {}

        lines = ["섹터\t거래대금합\t평균등락률\t확산도\t5분속도\t10분속도\t외인순매수\t기관순매수\t대장주"]
        for _, row in df.iterrows():
            val = float(row["거래대금합(억)"])
            txt_val = f"{val/10000:.1f}조" if val >= 10000 else f"{val:,.0f}억"
            diff = float(row.get("확산도(%)", 0))
            r5, r10 = vel_map.get(row["섹터명"], (float("nan"), float("nan")))
            txt5  = f"{r5:+.1f}%"  if not pd.isna(r5)  else "-"
            txt10 = f"{r10:+.1f}%" if not pd.isna(r10) else "-"
            lines.append(
                f"{row['섹터명']}\t{txt_val}\t"
                f"{float(row['평균등락률(%)']):+.1f}%\t"
                f"{diff:.0f}%\t"
                f"{txt5}\t{txt10}\t"
                f"{_fmt_money(float(row['외인순매수합(주)']))}\t"
                f"{_fmt_money(float(row['기관순매수합(주)']))}\t"
                f"{row['대장주']}"
            )
        QApplication.clipboard().setText("\n".join(lines))
        self.common_log("[섹터] 클립보드 복사 완료")

    def _on_sector_sort_toggle(self, checked: bool):
        self._sector_sort_mode = checked
        self.topTradingSectorSortPushButton.setText(f"섹터정렬 {'ON' if checked else 'OFF'}")

    def _on_top_trading_auto_toggle(self, checked: bool):
        if checked:
            interval_sec = self.topTradingIntervalSpinBox.value()
            self._top_trading_timer.start(interval_sec * 1000)
            self.topTradingAutoRefreshPushButton.setText(f"자동조회 ON ({interval_sec}초)")
            self._on_top_trading_refresh()   # 즉시 1회 조회
        else:
            self._top_trading_timer.stop()
            self.topTradingAutoRefreshPushButton.setText("자동조회 OFF")

    # 섹터별 배경 팔레트 (파스텔 계열, 8가지)
    _SECTOR_COLORS = [
        QColor(255, 218, 218),  # 연분홍
        QColor(218, 235, 255),  # 연하늘
        QColor(218, 255, 218),  # 연초록
        QColor(255, 248, 210),  # 연노랑
        QColor(238, 218, 255),  # 연보라
        QColor(255, 232, 210),  # 연주황
        QColor(210, 248, 248),  # 연청록
        QColor(248, 255, 218),  # 연연두
    ]
    _NXT_BG = QColor(255, 255, 180)

    def _on_sector_summary_updated(self, summary_df: pd.DataFrame) -> None:
        """SectorAnalyzer 콜백 — 섹터 요약 패널 갱신 + 매수 신호 갱신"""
        if hasattr(self, '_sector_flow_table'):
            self._update_sector_flow_table(summary_df)
        self._try_update_buy_signals()
        self._try_take_flow_snapshot(summary_df)
        if hasattr(self, '_dashboard'):
            self._dashboard.on_sector_update()

    def _on_score_updated(self, score_df: pd.DataFrame) -> None:
        """StockScorer 콜백 — Top 3 바 + 거래대금상위 테이블 점수/등급 셀 갱신."""
        # ── Top 3 바 갱신 ──────────────────────────────────────
        if hasattr(self, '_score_top3_label') and not score_df.empty:
            _RANK_STYLE = [
                "font-weight:bold; color:#cc0000;",   # 1위 — 빨강·볼드
                "color:#cc6600;",                      # 2위 — 주황
                "color:#0055cc;",                      # 3위 — 파랑
            ]
            top3 = score_df.head(3)
            parts = []
            for i, (_, row) in enumerate(top3.iterrows()):
                name  = row["종목명"] or row["종목코드"]
                score = int(round(row["score"]))
                style = _RANK_STYLE[i] if i < len(_RANK_STYLE) else ""
                parts.append(
                    f'<span style="{style}">{i+1}위 {name} {score}점</span>'
                )
            sep = '&nbsp;&nbsp;<span style="color:#ccc;">|</span>&nbsp;&nbsp;'
            self._score_top3_label.setText(sep.join(parts))

        if score_df.empty or not self._top_row_map:
            return
        table = self.topTradingTableWidget
        col_count = table.columnCount()
        # 점수/등급 컬럼 인덱스 찾기
        score_col = grade_col = -1
        for c in range(col_count):
            h = table.horizontalHeaderItem(c)
            if h is None:
                continue
            if h.text() == "점수":
                score_col = c
            elif h.text() == "등급":
                grade_col = c
        if score_col < 0:
            return
        _GRADE_COLOR = {"S": QColor(200, 0, 0), "A": QColor(200, 100, 0),
                        "B": QColor(0, 100, 180), "C": QColor(120, 120, 120),
                        "D": QColor(160, 160, 160)}
        for _, row in score_df.iterrows():
            code  = row["종목코드"]
            r_idx = self._top_row_map.get(code, -1)
            if r_idx < 0:
                continue
            score = row["score"]
            grade = row["등급"]
            color = _GRADE_COLOR.get(grade, QColor(Qt.black))
            if score_col >= 0:
                it = QTableWidgetItem(f"{score:.1f}")
                it.setTextAlignment(Qt.AlignCenter)
                it.setForeground(color)
                if grade == "S":
                    font = it.font(); font.setBold(True); it.setFont(font)
                table.setItem(r_idx, score_col, it)
            if grade_col >= 0:
                it = QTableWidgetItem(grade)
                it.setTextAlignment(Qt.AlignCenter)
                it.setForeground(color)
                if grade == "S":
                    font = it.font(); font.setBold(True); it.setFont(font)
                table.setItem(r_idx, grade_col, it)

        # StockScorer 업데이트 후에도 매수 신호 갱신 (tv_score 반영)
        self._try_update_buy_signals()

    def on_receive_top_trading_value(self, df):
        if df.empty:
            return

        # 섹터 집계기 / 점수 집계기 갱신 (UI 렌더링 전에 실행)
        try:
            self.sector_analyzer.update_trading(df)
        except Exception as e:
            logger.warning(f"[섹터집계] update_trading 오류: {e}")
        try:
            self.stock_scorer.update_trading(df)
        except Exception as e:
            logger.warning(f"[점수집계] update_trading 오류: {e}")

        # 상위 20개 종목 외인/기관 자동 조회 큐 추가
        try:
            top_codes = [str(c).strip() for c in df["종목코드"].head(20).tolist() if c]
            for _c in top_codes:
                if _c and _c not in self._investor_auto_queue:
                    self._investor_auto_queue.append(_c)
            if self._investor_auto_queue and not self._investor_auto_timer.isActive():
                self._investor_auto_timer.start()
        except Exception:
            pass

        score_map = self.stock_scorer.get_score_map()

        table = self.topTradingTableWidget

        col_keys = ["순위", "전일순위", "종목명", "업종", "현재가", "전일대비", "등락률",
                    "매도호가", "매수호가", "거래량", "시가총액", "거래대금", "점수", "등급"]
        headers = ["순위", "전일순위", "종목명", "업종", "현재가", "전일대비", "등락률(%)",
                   "매도호가", "매수호가", "거래량", "시가총액(억)", "거래대금(억)", "점수", "등급"]
        table.setColumnCount(len(col_keys))
        table.setHorizontalHeaderLabels(headers)

        # 섹터정렬 ON: 섹터 거래대금합 내림차순 → 섹터 내 거래대금 내림차순
        has_sector = "업종" in df.columns and df["업종"].notna().any()
        if self._sector_sort_mode and has_sector:
            sector_total = df.groupby("업종")["거래대금"].sum()
            df = df.copy()
            df["_sr"] = df["업종"].map(sector_total).fillna(0)
            df = df.sort_values(["_sr", "거래대금"], ascending=[False, False]).drop(columns=["_sr"])
            df = df.reset_index(drop=True)

        # 섹터별 첫 번째 종목(대장주) 행번호 집합 (0-based)
        leading_rows: set[int] = set()
        sector_color_map: dict[str, QColor] = {}
        if has_sector:
            seen: set[str] = set()
            color_idx = 0
            for row_idx, (_, row) in enumerate(df.iterrows()):
                s = row.get("업종", "") or ""
                if s and s not in seen:
                    seen.add(s)
                    sector_color_map[s] = self._SECTOR_COLORS[color_idx % len(self._SECTOR_COLORS)]
                    color_idx += 1
                    if self._sector_sort_mode:
                        leading_rows.add(row_idx)

        _GRADE_COLOR = {"S": QColor(200, 0, 0), "A": QColor(200, 100, 0),
                        "B": QColor(0, 100, 180), "C": QColor(120, 120, 120),
                        "D": QColor(160, 160, 160)}

        new_row_map: dict[str, int] = {}
        table.setRowCount(len(df))
        for row_idx, (_, row) in enumerate(df.iterrows()):
            code       = str(row.get("종목코드", ""))
            is_nxt     = bool(row.get("NXT", False))
            sector     = row.get("업종", "") or ""
            is_leading = row_idx in leading_rows
            bg_color   = self._NXT_BG if is_nxt else sector_color_map.get(sector)
            score, grade = score_map.get(code, (None, ""))
            new_row_map[code] = row_idx

            for col_idx, col in enumerate(col_keys):
                if col in ("순위", "전일순위"):
                    text  = str(row[col])
                    align = Qt.AlignCenter
                elif col == "종목명":
                    prefix = "★" if is_leading else ""
                    text   = f"{prefix}{row['종목명']}"
                    align  = Qt.AlignLeft | Qt.AlignVCenter
                elif col == "업종":
                    text  = sector
                    align = Qt.AlignLeft | Qt.AlignVCenter
                elif col in ("현재가", "매도호가", "매수호가", "거래량"):
                    text  = f"{row[col]:,}"
                    align = Qt.AlignRight | Qt.AlignVCenter
                elif col == "전일대비":
                    text  = f"{row[col]:+,}"
                    align = Qt.AlignRight | Qt.AlignVCenter
                elif col == "등락률":
                    text  = f"{row[col]:.2f}%"
                    align = Qt.AlignRight | Qt.AlignVCenter
                elif col == "시가총액":
                    text  = f"{row[col]:,}억"
                    align = Qt.AlignRight | Qt.AlignVCenter
                elif col == "거래대금":
                    text  = f"{row[col] // 100:,}억"
                    align = Qt.AlignRight | Qt.AlignVCenter
                elif col == "점수":
                    text  = f"{score:.1f}" if score is not None else "-"
                    align = Qt.AlignCenter
                elif col == "등급":
                    text  = grade
                    align = Qt.AlignCenter
                else:
                    text  = ""
                    align = Qt.AlignCenter

                item = QTableWidgetItem(text)
                item.setTextAlignment(align)

                if col == "종목명":
                    item.setData(Qt.UserRole, row["종목코드"])
                    if is_leading:
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)

                if bg_color:
                    item.setBackground(bg_color)

                if col in ("전일대비", "등락률"):
                    try:
                        v = float(row["등락률"])
                        if v > 0:
                            item.setForeground(QColor(Qt.red))
                        elif v < 0:
                            item.setForeground(QColor(Qt.blue))
                    except (ValueError, TypeError):
                        pass
                elif col in ("점수", "등급") and grade in _GRADE_COLOR:
                    item.setForeground(_GRADE_COLOR[grade])
                    if grade == "S":
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)

                table.setItem(row_idx, col_idx, item)

        self._top_row_map = new_row_map
        table.resizeColumnsToContents()

    # ========================
    # 신호등 판단 (돌고래 전략 핵심)
    # ========================

    def _get_signal_zone(self, stock_code: str, 현재가: float) -> str:
        """
        🟢 초록불: 전일 종가 위 → 적극 트레이딩
        🟡 노란불: 전일 시가~종가 사이 → 신중
        🔴 빨간불: 전일 시가 아래 → 매매 금지, 보유 시 즉시 청산
        ⚪ 데이터 없음
        """
        p = self.prev_day_data.get(stock_code)
        if not p:
            return "⚪"
        if 현재가 >= p['초록선']:
            return "🟢"
        elif 현재가 >= p['빨간선']:
            return "🟡"
        return "🔴"

    # ========================
    # UI에서 운영 시간 읽기
    # ========================

    def _get_trade_start_time(self) -> datetime.datetime:
        t = self.tradeStartTimeEdit.time()
        return datetime.datetime.now().replace(
            hour=t.hour(), minute=t.minute(), second=0, microsecond=0)

    def _get_trade_end_time(self) -> datetime.datetime:
        t = self.tradeEndTimeEdit.time()
        return datetime.datetime.now().replace(
            hour=t.hour(), minute=t.minute(), second=0, microsecond=0)

    # ========================
    # 매수/매도 수량 계산
    # ========================

    def _calc_buy_quantity(self, current_price: int) -> int:
        if self.buyQuantityRadioButton.isChecked():
            return self.buyQuantitySpinBox.value()
        amount_text = self.customBuyAmountLineEdit.text().replace(',', '')
        try:
            amount = int(amount_text)
        except ValueError:
            return 0
        if current_price <= 0:
            return 0
        return amount // current_price

    def _calc_sell_quantity(self, available_qty: int) -> int:
        if self.sellQuantityRadioButton.isChecked():
            return self.sellQuantitySpinBox.value()
        ratio = self.sellAvailableRatioSpinBox.value() / 100.0
        return max(1, int(available_qty * ratio))

    # ========================
    # 주문 큐 처리
    # ========================

    @log_exceptions
    def _process_orders_queue(self):
        if self.orders_queue.empty():
            return
        order = self.orders_queue.get()
        sRQName = order[0]
        if sRQName in ("시장가매수주문", "지정가매수주문"):
            _, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, _, _, _ = order
            self.send_order(sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, "")
        elif sRQName in ("시장가매도주문", "지정가매도주문"):
            _, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, _, _, _ = order
            self.send_order(sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, "")
        elif sRQName in ("매수취소주문", "매도취소주문"):
            _, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, _, _, sOrgOrderNo = order
            self.send_order(sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sOrgOrderNo)
        elif sRQName in ("매수정정주문", "매도정정주문"):
            _, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, _, _, _, sOrgOrderNo = order
            self.send_order(sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sOrgOrderNo)
        elif sRQName in ("지정가신용매수주문",):
            _, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, _, _, _, sCreditGb, sLoanDate, _ = order
            self.send_credit_order(sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sCreditGb, sLoanDate, "")
        elif sRQName in ("지정가신용매도주문",):
            _, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sCreditGb, sLoanDate, _ = order
            self.send_credit_order(sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sCreditGb, sLoanDate, "")

    # ========================
    # 자동매매 ON/OFF
    # ========================

    def auto_trade_on(self):
        now = time.time()
        if not self.can_push_auto_trade_on_btn:
            return
        if now - self.last_auto_trade_on_unix_time < 10:
            return
        self.last_auto_trade_on_unix_time = now
        self.can_push_auto_trade_on_btn = False
        self.is_no_transaction = False
        self.common_log("자동매매 시작")

    def auto_trade_off(self):
        self.is_no_transaction = True
        self.can_push_auto_trade_on_btn = True
        self.common_log("자동매매 중지")

    # ========================
    # 미체결 주문 관리
    # ========================

    @log_exceptions
    def check_unfinished_orders(self):
        now_time = datetime.datetime.now()
        if self.is_no_transaction:
            return
        if not (self._get_trade_start_time() <= now_time <= self._get_trade_end_time()):
            return
        self.order_check_timer.stop()
        limit_seconds = self.unfilledOrderSecondsSpinBox.value()
        action        = self.unfilledOrderActionComboBox.currentText()
        exchange      = 'KRX' if self.is_paper_trading else 'SOR'
        account_num   = self.get_trading_account_num()
        try:
            for 주문번호, row in self.unfinished_orders_df.copy(deep=True).iterrows():
                try:
                    종목코드    = row['종목코드']
                    주문체결시간 = row['주문체결시간']
                    미체결수량  = row['미체결수량']
                    주문구분    = row['주문구분']
                    if 미체결수량 == 0:
                        self.unfinished_orders_df.drop(주문번호, inplace=True)
                        continue
                    order_time = datetime.datetime.strptime(
                        datetime.datetime.now().strftime('%Y%m%d') + str(주문체결시간).zfill(6),
                        '%Y%m%d%H%M%S'
                    )
                    if (now_time - order_time).total_seconds() < limit_seconds:
                        continue

                    if action == "정정" and 주문구분 == "매수":
                        현재가 = self.stock_code_to_realtime_price_dict.get(종목코드, 0)
                        if 현재가 > 0:
                            self.enqueue_amend_order(
                                종목코드, account_num, order_quantity=미체결수량,
                                order_price=현재가, order_type='매수정정',
                                exchange=exchange, order_num=주문번호,
                            )
                            self.unfinished_orders_df.drop(주문번호, inplace=True)
                    elif 주문구분 == "매수":
                        self.enqueue_cancel_order(
                            종목코드, account_num, order_type='매수취소',
                            order_quantity=미체결수량, order_num=주문번호, exchange=exchange,
                        )
                        self.unfinished_orders_df.drop(주문번호, inplace=True)
                        if (종목코드 in self.auto_trade_stock_df.index
                                and self.auto_trade_stock_df.at[종목코드, "매수주문완료"]):
                            self.auto_trade_stock_df.drop(종목코드, inplace=True)
                    elif 주문구분 == "매도":
                        self.enqueue_cancel_order(
                            종목코드, account_num, order_type='매도취소',
                            order_quantity=미체결수량, order_num=주문번호, exchange=exchange,
                        )
                        self.unfinished_orders_df.drop(주문번호, inplace=True)
                        if 종목코드 in self.auto_trade_stock_df.index:
                            self.auto_trade_stock_df.at[종목코드, "매도주문완료"] = False
                except Exception as e:
                    logger.exception(e)
        except Exception as e:
            logger.exception(e)
        self.order_check_timer.start(1000)

    # ========================
    # 자동매매 테이블 클릭
    # ========================

    @log_exceptions
    def on_auto_trade_table_view_clicked(self, index):
        column_name = self.auto_trade_stock_df.columns[index.column()]
        if column_name == "삭제":
            stock_code = self.auto_trade_stock_df.index[index.row()]
            self.auto_trade_stock_df.drop(stock_code, inplace=True)
            self.update_pandas_models()

    # ========================
    # 자동매매 DataFrame 로드
    # ========================

    def load_auto_trader_df(self):
        pkl_path = os.path.join(data_save_path, 'auto_trade_stock_df.pkl')
        columns = ["종목코드", "종목명", "현재가", "매매가능수량", "수익률(%)",
                   "이평선", "매수주문완료", "매도주문완료", "삭제"]
        if os.path.exists(pkl_path):
            try:
                return pd.read_pickle(pkl_path)
            except Exception as e:
                logger.exception(e)
        df = pd.DataFrame(columns=columns)
        df.set_index("종목코드", inplace=True)
        return df

    # ========================
    # 계좌 잔고 수신
    # ========================

    @log_exceptions
    def on_opw00018_req(self, trcode, rqname):
        data_cnt = self._get_repeat_cnt(trcode, rqname)
        for i in range(data_cnt):
            종목코드   = self._comm_get_data(trcode, "", rqname, i, "종목코드").replace("A", "").strip()
            종목명    = self._comm_get_data(trcode, "", rqname, i, "종목명").strip()
            보유수량   = int(self._comm_get_data(trcode, "", rqname, i, "보유수량") or 0)
            매매가능수량 = int(self._comm_get_data(trcode, "", rqname, i, "매매가능수량") or 0)
            평균단가   = int(self._comm_get_data(trcode, "", rqname, i, "평균단가") or 0)
            현재가    = int(abs(float(self._comm_get_data(trcode, "", rqname, i, "현재가") or 0)))
            key = self.get_account_key(self.using_account_num, 종목코드)
            if 보유수량 > 0 and 종목코드:
                self.account_info_df.loc[key] = {
                    "계좌번호": self.using_account_num, "종목코드": 종목코드,
                    "종목명": 종목명, "보유수량": 보유수량, "매매가능수량": 매매가능수량,
                    "평균단가": 평균단가, "현재가": 현재가,
                    "전일대비(%)": None, "수익률(%)": None,
                }
        if not self.has_done_loading:
            self.on_done_loading_basic_info()

    # ========================
    # 로딩 완료 후 초기화
    # ========================

    def closeEvent(self, event) -> None:
        """프로그램 종료 시 DB 백업 후 연결 종료."""
        try:
            self.db.backup_db()
            self.db.close()
        except Exception as e:
            logger.exception(f"[DB] 종료 처리 실패: {e}")
        super().closeEvent(event)

    @log_exceptions
    def on_done_loading_basic_info(self):
        self.has_done_loading = True
        self.backup_timer.timeout.connect(self.backup_data)
        self.backup_timer.start(5000)
        # 기본 정보 로딩 완료 후 거래대금상위 자동 조회 (3초 지연)
        QTimer.singleShot(3000, self._on_top_trading_refresh)
        self.table_refresh_timer.timeout.connect(self.update_pandas_models)
        self.table_refresh_timer.start(1000)
        self.order_check_timer.timeout.connect(self.check_unfinished_orders)
        self.order_check_timer.start(1000)
        # 계좌번호 콤보박스
        self.customAccountNumComboBox.clear()
        for acc in self.account_list:
            self.customAccountNumComboBox.addItem(acc)
        # 신호등 데이터 확보: 추적 종목별 일봉 요청
        for stock_code in self.auto_trade_stock_df.index:
            self.request_candle_data(stock_code, candle_type="일봉", include_pre_post=False)
        # 시장 지수 실시간 등록
        futures_code = self._get_front_month_futures_code()
        logger.info(f"[시장차트] 선물 코드: {futures_code}")
        self.register_market_indices(futures_code=futures_code)
        # 해외선물 분봉 조회 + 실시간 등록 (5초 지연 — TR 큐 여유)
        QTimer.singleShot(5000, self._request_overseas_futures)
        # DB: 보유종목 불러오기
        self._load_holding_from_db()
        self.common_log("기본 정보 로딩 완료!")

    # ========================
    # 일봉 데이터 수신 → 전일 OHLC 저장 (신호등 기법)
    # ========================

    @log_exceptions
    def on_receive_candle_data(self, stock_code, df, chart_type=''):
        # ── 일봉: 전일 고가/종가/시가 저장 후 리턴
        if chart_type == "일봉":
            if len(df) >= 2:
                yesterday = df.iloc[-2]  # 마지막이 오늘, 그 전이 전일
                self.prev_day_data[stock_code] = {
                    '블랙선': int(yesterday['High']),   # 전일 고가
                    '초록선': int(yesterday['Close']),  # 전일 종가
                    '빨간선': int(yesterday['Open']),   # 전일 시가
                }
                p = self.prev_day_data[stock_code]
                logger.info(
                    f"[신호등 데이터] {stock_code} 블랙선={p['블랙선']:,} "
                    f"초록선={p['초록선']:,} 빨간선={p['빨간선']:,}"
                )
            return

        # ── 분봉: 이평선 + 신호등 매매 로직
        selected_candle = self.candleTypeComboBox.currentText()
        if chart_type == "분봉" and selected_candle == "일봉":
            return
        if chart_type == "일봉" and selected_candle != "일봉":
            return

        if stock_code not in self.auto_trade_stock_df.index:
            return

        ma_length = self.maLengthSpinBox.value()
        if len(df) < ma_length:
            return

        df = df.copy()
        df['MA'] = df['Close'].rolling(window=ma_length).mean()
        ma      = df['MA'].iloc[-1]
        현재가   = int(df['Close'].iloc[-1])
        신호등   = self._get_signal_zone(stock_code, 현재가)
        p        = self.prev_day_data.get(stock_code)
        블랙선   = p['블랙선'] if p else None
        초록선   = p['초록선'] if p else None
        빨간선   = p['빨간선'] if p else None

        condition      = self.maConditionComboBox.currentText()
        buy_signal_ma  = (현재가 >= ma) if condition == "이상" else (현재가 <= ma)
        account_num    = self.get_trading_account_num()
        key            = self.get_account_key(account_num, stock_code)
        exchange       = 'KRX' if self.is_paper_trading else 'SOR'
        is_buy_market  = self.buyMarketRadioButton.isChecked()
        is_sell_market = self.sellMarketRadioButton.isChecked()
        has_position   = key in self.account_info_df.index

        # ── 매수 조건
        # 신호등 데이터 있으면: 🟢 구간만 매수 허용
        # 신호등 데이터 없으면: MA 조건만으로 판단
        buy_zone_ok = (신호등 == "🟢") if p else True
        if (buy_zone_ok
                and buy_signal_ma
                and not self.auto_trade_stock_df.at[stock_code, "매수주문완료"]
                and not self.is_no_transaction):
            qty = self._calc_buy_quantity(현재가)
            if qty > 0:
                buy_price = 0 if is_buy_market else 현재가 + self.buyOffsetSpinBox.value()
                self.enqueue_buy_order(
                    stock_code, account_num=account_num, is_market_order=is_buy_market,
                    order_quantity=qty, order_price=buy_price, exchange=exchange,
                )
                self.auto_trade_stock_df.at[stock_code, "매수주문완료"] = True
                logger.info(
                    f"[{신호등} 매수] {stock_code} 현재가={현재가:,} "
                    f"MA({ma_length})={ma:.0f} 초록선={초록선}"
                )

        if not has_position or self.auto_trade_stock_df.at[stock_code, "매도주문완료"]:
            return
        available = int(self.account_info_df.at[key, "보유수량"])
        if available <= 0:
            return

        def _do_sell(reason: str):
            qty = self._calc_sell_quantity(available)
            sell_price = 0 if is_sell_market else 현재가 + self.sellOffsetSpinBox.value()
            self.enqueue_sell_order(
                stock_code, account_num=account_num, is_market_order=is_sell_market,
                order_quantity=qty, order_price=sell_price, exchange=exchange,
            )
            self.auto_trade_stock_df.at[stock_code, "매도주문완료"] = True
            logger.info(f"[{reason}] {stock_code} 현재가={현재가:,}")

        # ── 매도 조건 ① 블랙선(전일 고가) 도달 → 익절
        if 블랙선 and 현재가 >= 블랙선 and not self.is_no_transaction:
            _do_sell(f"🎯 블랙선 익절 블랙선={블랙선:,}")

        # ── 매도 조건 ② 🔴 빨간불 (전일 시가 이탈) → 즉시 청산
        elif 빨간선 and 현재가 < 빨간선 and not self.is_no_transaction:
            _do_sell(f"🔴 빨간불 청산 빨간선={빨간선:,}")

        # ── 매도 조건 ③ 초록선(전일 종가) 이탈 → 청산
        elif 초록선 and 현재가 < 초록선 and not self.is_no_transaction:
            _do_sell(f"🟡 초록선 이탈 청산 초록선={초록선:,}")

    # ========================
    # 실시간 틱 수신 — 기계적 손절 (-2%) + 수익률 매도
    # ========================

    @log_exceptions
    def on_receive_realtime_tick_data(self, data):
        super().on_receive_realtime_tick_data(data)

        # 점수 집계기 / 섹터 집계기 실시간 갱신 (체결강도·거래량·등락률)
        _code = data.get('종목코드', '')
        if _code:
            self.stock_scorer.update_realtime(
                _code,
                change_pct    = data.get('등락률'),
                volume        = data.get('거래량'),
                exec_strength = data.get('체결강도'),
            )
            _es = data.get('체결강도')
            if _es is not None:
                self.sector_analyzer.update_exec_strength(_code, _es)

        if self.is_no_transaction:
            return
        종목코드    = data['종목코드']
        현재가     = data['현재가']
        account_num = self.get_trading_account_num()
        key         = self.get_account_key(account_num, 종목코드)
        exchange    = 'KRX' if self.is_paper_trading else 'SOR'

        if key not in self.account_info_df.index:
            return
        if 종목코드 not in self.auto_trade_stock_df.index:
            return
        if self.auto_trade_stock_df.at[종목코드, "매도주문완료"]:
            return

        수익률     = self.account_info_df.at[key, "수익률(%)"]
        available   = int(self.account_info_df.at[key, "보유수량"])
        is_sell_market = self.sellMarketRadioButton.isChecked()

        if 수익률 is None or available <= 0:
            return

        stop_loss_pct = self.profitSellLowerSpinBox.value()   # 기본 -2.0 (설정에서 -2로 변경 권장)
        upper_pct     = self.profitSellUpperSpinBox.value()   # 보조 익절 기준

        def _sell_by_profit(reason: str):
            qty = self._calc_sell_quantity(available)
            sell_price = 0 if is_sell_market else int(현재가) + self.sellOffsetSpinBox.value()
            self.enqueue_sell_order(
                종목코드, account_num, is_market_order=is_sell_market,
                order_quantity=qty, order_price=sell_price, exchange=exchange,
            )
            self.auto_trade_stock_df.at[종목코드, "매도주문완료"] = True
            logger.info(f"[{reason}] {종목코드} 수익률={수익률:.2f}% 현재가={현재가:,}")

        # ── 기계적 손절: 수익률 ≤ profitSellLowerSpinBox (권장 -2%)
        if 수익률 <= stop_loss_pct:
            _sell_by_profit(f"🛑 -{abs(stop_loss_pct):.1f}% 기계적 손절")

        # ── 수익률 상한 매도 (보조)
        elif 수익률 >= upper_pct:
            _sell_by_profit(f"💰 +{upper_pct:.1f}% 수익률 익절")

    # ========================
    # 데이터 백업
    # ========================

    def backup_data(self):
        try:
            pkl_path = os.path.join(data_save_path, 'auto_trade_stock_df.pkl')
            self.auto_trade_stock_df.to_pickle(pkl_path)
        except Exception as e:
            logger.exception(e)

    # ========================
    # DB 영속성 메서드
    # ========================

    def _load_holding_from_db(self) -> None:
        """프로그램 시작 시 DB holding 테이블을 불러와 로그로 표시."""
        try:
            holdings = self.db.get_holding_list()
            if not holdings:
                logger.info("[DB] 보유종목 없음 (holding 테이블 비어 있음)")
                return
            logger.info(f"[DB] 보유종목 {len(holdings)}개 불러옴")
            for h in holdings:
                logger.info(
                    f"  {h['code']} {h['name']} | 진입 {h['entry_date']} "
                    f"@ {h['entry_price']:,} | 상태 {h['status']}"
                )
            self.common_log(f"[DB] 보유종목 {len(holdings)}개 복원 완료")
        except Exception as e:
            logger.exception(f"[DB] _load_holding_from_db 실패: {e}")

    def _save_supply_snapshot(self) -> None:
        """1분마다 호출 — leader_scores 기준 수급흐름 배치 저장.

        장 운영 시간(08:30~16:00)에만 저장.
        """
        now = datetime.datetime.now()
        if not (8 <= now.hour < 16 or (now.hour == 8 and now.minute >= 30)):
            return

        try:
            leader_df = self.sector_analyzer.get_leader_scores()
        except Exception:
            return
        if leader_df is None or leader_df.empty:
            return

        date_str = now.strftime("%Y%m%d")
        time_str = now.strftime("%H%M")
        rows: list[dict] = []

        for _, row in leader_df.iterrows():
            code = str(row.get("종목코드", "")).strip()
            if not code:
                continue
            foreign_raw = row.get("외인순매수", None)
            try:
                foreigner_buy = float(foreign_raw) / 100.0 if foreign_raw is not None else None
            except (TypeError, ValueError):
                foreigner_buy = None

            price_raw = (
                self.stock_code_to_realtime_price_dict.get(code)
                or row.get("현재가")
            )
            try:
                price = float(price_raw) if price_raw is not None else None
            except (TypeError, ValueError):
                price = None

            rows.append({
                "date":          date_str,
                "time":          time_str,
                "code":          code,
                "name":          str(row.get("종목명", "")),
                "sector":        str(row.get("섹터명", "")),
                "foreigner_buy": foreigner_buy,
                "program_buy":   None,
                "volume":        None,
                "price":         price,
                "source":        "nxt" if self.is_paper_trading is False else "regular",
            })

        if rows:
            self.db.save_supply_batch(rows)
            logger.debug(f"[DB] supply_flow {len(rows)}건 저장 ({time_str})")

        # 자금흐름 모니터에 실시간 데이터 전달
        if self._flow_monitor_win and self._flow_monitor_win.isVisible():
            try:
                from flow_monitor import calc_score, calc_temperature
                sectors: dict = {}
                stocks:  dict = {}
                try:
                    summary_df = self.sector_analyzer.get_summary()
                    for rank, (_, sr) in enumerate(summary_df.iterrows(), 1):
                        sname = str(sr.get("섹터명", ""))
                        fore  = float(sr.get("외인순매수합", 0) or 0) / 100.0
                        prog  = float(sr.get("프로그램합",   0) or 0) / 100.0
                        vol   = float(sr.get("거래대금증가율", 0) or 0)
                        sectors[sname] = {
                            "rank":        rank,
                            "foreigner":   fore,
                            "program":     prog,
                            "vol_change":  vol,
                            "temperature": calc_temperature(fore, prog, vol),
                        }
                except Exception:
                    pass
                leader_df2 = self.sector_analyzer.get_leader_scores()
                if leader_df2 is not None and not leader_df2.empty:
                    for _, row2 in leader_df2.iterrows():
                        code2 = str(row2.get("종목코드", ""))
                        if not code2:
                            continue
                        f = float(row2.get("외인순매수", 0) or 0) / 100.0
                        p = 0.0
                        vr = float(row2.get("거래대금비율", 1.0) or 1.0)
                        stocks[code2] = {
                            "name":       str(row2.get("종목명", "")),
                            "code":       code2,
                            "sector":     str(row2.get("섹터명", "")),
                            "price":      float(self.stock_code_to_realtime_price_dict.get(code2) or 0),
                            "change_pct": float(row2.get("등락률(%)", 0) or 0),
                            "foreigner":  f,
                            "program":    p,
                            "vol_ratio":  vr,
                            "consec_buy": 0,
                            "prog_accel": False,
                            "is_top":     False,
                            "score":      calc_score(f, p, vr),
                        }
                self._flow_monitor_win.feed_data({
                    "sectors":  sectors,
                    "stocks":   stocks,
                    "time":     time_str,
                    "datetime": now,
                    "is_demo":  False,
                })
            except Exception as e:
                logger.debug(f"[FlowMonitor] feed_data 실패: {e}")

    def _save_nxt_snapshot(self) -> None:
        """20:00 — 장후 NXT 수급 데이터 저장.

        leader_scores의 외인순매수를 정규장 값으로, 실시간 누적 마지막 값을
        nxt_fore로 저장하고 verdict를 판정함.
        """
        try:
            leader_df = self.sector_analyzer.get_leader_scores()
        except Exception:
            return
        if leader_df is None or leader_df.empty:
            return

        date_str  = datetime.date.today().strftime("%Y%m%d")
        saved_cnt = 0

        for _, row in leader_df.iterrows():
            code = str(row.get("종목코드", "")).strip()
            if not code:
                continue
            try:
                regular_fore = float(row.get("외인순매수", 0) or 0) / 100.0
            except (TypeError, ValueError):
                regular_fore = 0.0

            # NXT 추가 매수는 별도 실시간 집계가 필요하므로 현재는 동일값 저장
            # (추후 NXT 전용 실시간 FID 집계로 교체)
            nxt_fore = regular_fore
            nxt_gap  = nxt_fore - regular_fore

            if   nxt_gap >  0:        verdict = "보유"
            elif nxt_gap < -50.0:     verdict = "청산"
            else:                      verdict = "재확인"

            self.db.save_nxt({
                "date":         date_str,
                "code":         code,
                "name":         str(row.get("종목명", "")),
                "regular_fore": regular_fore,
                "nxt_fore":     nxt_fore,
                "nxt_gap":      nxt_gap,
                "verdict":      verdict,
                "next_open":    None,
                "next_pct":     None,
            })
            saved_cnt += 1

        logger.info(f"[DB] NXT 수급 {saved_cnt}건 저장 완료")
        self.common_log(f"[DB] NXT 수급 {saved_cnt}건 저장")

    def _update_nxt_next_day(self) -> None:
        """09:05 — 전일 nxt_data의 next_open / next_pct 갱신.

        현재가(실시간) 기준으로 등락률을 계산해 업데이트.
        실시간 가격이 없는 종목은 건너뜀.
        """
        try:
            pending = self.db.get_nxt_yesterday()
        except Exception as e:
            logger.exception(f"[DB] get_nxt_yesterday 실패: {e}")
            return
        if not pending:
            return

        updated = 0
        for rec in pending:
            code      = rec["code"]
            nxt_fore  = rec.get("nxt_fore") or 0.0
            curr_price = self.stock_code_to_realtime_price_dict.get(code)
            if curr_price is None or curr_price == 0:
                continue
            entry_price = rec.get("regular_fore")   # 수급 기준이므로 진입가 없음
            # next_pct: nxt_fore 대비 현재가 비율 (수급점수 대리값으로 활용)
            # 실제 시가 대비 등락률을 구하려면 opt10001 조회가 필요하나
            # 09:05에는 실시간 현재가가 곧 시가에 근사
            self.db.update_nxt_next_day(
                date      = rec["date"],
                code      = code,
                next_open = float(curr_price),
                next_pct  = 0.0,   # 실시간 등락률 FID 20 수신 시 보완
            )
            updated += 1

        logger.info(f"[DB] next_day 업데이트 {updated}건 완료")

    # ========================
    # Pandas 모델 업데이트
    # ========================

    def update_pandas_models(self):
        try:
            self.auto_pd_model.refresh()
            self.account_pd_model.refresh()
            self.credit_pd_model.refresh()
        except Exception as e:
            logger.exception(e)

    def update_cell(self, key, column_name):
        self.update_pandas_models()

    def update_credit_cell(self, key, column_name):
        self.update_pandas_models()

    def clear_auto_sell_df_by_stock_code(self, stock_code, target_account='현금', account_num=''):
        if stock_code in self.auto_trade_stock_df.index:
            self.auto_trade_stock_df.at[stock_code, "매도주문완료"] = False

    # ========================
    # 설정 저장/불러오기
    # ========================

    def on_save_settings(self):
        s = self.settings
        s.setValue('autoShutDownTimeEdit',        self.autoShutDownTimeEdit.time().toString("HHmmss"))
        s.setValue('autoOnCheckBox',              self.autoOnCheckBox.isChecked())
        s.setValue('tradeStartTimeEdit',          self.tradeStartTimeEdit.time().toString("HHmmss"))
        s.setValue('tradeEndTimeEdit',            self.tradeEndTimeEdit.time().toString("HHmmss"))
        s.setValue('profitSellUpperSpinBox',      self.profitSellUpperSpinBox.value())
        s.setValue('profitSellLowerSpinBox',      self.profitSellLowerSpinBox.value())
        s.setValue('maxTrackingCountSpinBox',     self.maxTrackingCountSpinBox.value())
        s.setValue('candleTypeComboBox',          self.candleTypeComboBox.currentIndex())
        s.setValue('maLengthSpinBox',             self.maLengthSpinBox.value())
        s.setValue('maConditionComboBox',         self.maConditionComboBox.currentIndex())
        s.setValue('unfilledOrderSecondsSpinBox', self.unfilledOrderSecondsSpinBox.value())
        s.setValue('unfilledOrderActionComboBox', self.unfilledOrderActionComboBox.currentIndex())
        s.setValue('buyMarketRadioButton',        self.buyMarketRadioButton.isChecked())
        s.setValue('buyOffsetSpinBox',            self.buyOffsetSpinBox.value())
        s.setValue('buyQuantityRadioButton',      self.buyQuantityRadioButton.isChecked())
        s.setValue('buyQuantitySpinBox',          self.buyQuantitySpinBox.value())
        s.setValue('sellMarketRadioButton',       self.sellMarketRadioButton.isChecked())
        s.setValue('sellOffsetSpinBox',           self.sellOffsetSpinBox.value())
        s.setValue('sellQuantityRadioButton',     self.sellQuantityRadioButton.isChecked())
        s.setValue('sellQuantitySpinBox',         self.sellQuantitySpinBox.value())
        s.setValue('sellAvailableRatioSpinBox',    self.sellAvailableRatioSpinBox.value())
        s.setValue('topTradingFilterComboBox',     self.topTradingFilterComboBox.currentIndex())
        s.setValue('topTradingIntervalSpinBox',   self.topTradingIntervalSpinBox.value())
        s.setValue('investorAmountRadioButton',   self.investorAmountRadioButton.isChecked())
        s.setValue('db_path',                     self.db.db_path)
        logger.info(f"[설정] DB 경로 저장: {self.db.db_path}")

    def load_trading_settings(self):
        s = self.settings
        self.tradeStartTimeEdit.setTime(
            QTime.fromString(s.value('tradeStartTimeEdit', "090000"), "HHmmss"))
        self.tradeEndTimeEdit.setTime(
            QTime.fromString(s.value('tradeEndTimeEdit', "133000"), "HHmmss"))
        self.profitSellUpperSpinBox.setValue(s.value('profitSellUpperSpinBox', 1.6, float))
        self.profitSellLowerSpinBox.setValue(s.value('profitSellLowerSpinBox', -2.0, float))
        self.maxTrackingCountSpinBox.setValue(s.value('maxTrackingCountSpinBox', 100, int))
        self.candleTypeComboBox.setCurrentIndex(s.value('candleTypeComboBox', 0, int))
        self.maLengthSpinBox.setValue(s.value('maLengthSpinBox', 5, int))
        self.maConditionComboBox.setCurrentIndex(s.value('maConditionComboBox', 0, int))
        self.unfilledOrderSecondsSpinBox.setValue(s.value('unfilledOrderSecondsSpinBox', 60, int))
        self.unfilledOrderActionComboBox.setCurrentIndex(s.value('unfilledOrderActionComboBox', 0, int))
        if s.value('buyMarketRadioButton', False, bool):
            self.buyMarketRadioButton.setChecked(True)
        self.buyOffsetSpinBox.setValue(s.value('buyOffsetSpinBox', 0, int))
        if s.value('buyQuantityRadioButton', False, bool):
            self.buyQuantityRadioButton.setChecked(True)
        self.buyQuantitySpinBox.setValue(s.value('buyQuantitySpinBox', 10, int))
        if s.value('sellMarketRadioButton', False, bool):
            self.sellMarketRadioButton.setChecked(True)
        self.sellOffsetSpinBox.setValue(s.value('sellOffsetSpinBox', 0, int))
        if s.value('sellQuantityRadioButton', False, bool):
            self.sellQuantityRadioButton.setChecked(True)
        self.sellQuantitySpinBox.setValue(s.value('sellQuantitySpinBox', 10, int))
        self.sellAvailableRatioSpinBox.setValue(s.value('sellAvailableRatioSpinBox', 100.0, float))
        # ETF+ETN 제외(1)를 기본값으로 설정
        self.topTradingFilterComboBox.setCurrentIndex(s.value('topTradingFilterComboBox', 1, int))
        self.topTradingIntervalSpinBox.setValue(s.value('topTradingIntervalSpinBox', 60, int))
        if s.value('investorAmountRadioButton', True, bool):
            self.investorAmountRadioButton.setChecked(True)
        else:
            self.investorQuantityRadioButton.setChecked(True)

    @log_exceptions
    def on_finished_password_settings(self):
        logger.debug(f"계좌번호: {self.using_account_num} 정보 조회를 시작합니다.")
        self.load_trading_settings()
        self.request_get_account_balance()

    def update_custom_line_edit_masking(self):
        is_masked = self.maskAccountCheckBox.isChecked()
        if is_masked:
            from utils import MaskedComboBoxDelegate
            delegate = MaskedComboBoxDelegate(self)
            self.customAccountNumComboBox.setItemDelegate(delegate)
        else:
            self.customAccountNumComboBox.setItemDelegate(None)

    # ========================
    # 해외선물 탭
    # ========================

    def _request_overseas_futures(self):
        """해외선물 분봉 조회 + 실시간 등록."""
        code = self._ovs_code.strip().upper()
        if not code:
            return
        self.tr_req_queue.put([self.request_opt50028, code, "1"])
        self.register_overseas_futures(code)
        logger.info(f"[해외선물] {code} 분봉 요청 + 실시간 등록")

    def _setup_overseas_futures_tab(self):
        from PyQt5.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QLabel,
            QLineEdit, QPushButton, QTableWidget, QHeaderView, QFrame,
        )

        _DARK = "#1e1e1e"
        _TBL_SS = (
            "QTableWidget { background: #1e1e1e; color: #e0e0e0; border: none; "
            "  gridline-color: #333; }"
            "QTableWidget::item { padding: 1px 5px; }"
            "QHeaderView::section { background: #2a2a2a; color: #aaa; border: none;"
            "  border-bottom: 1px solid #444; padding: 2px 5px; font-size: 11px; }"
            "QTableWidget::item:selected { background: #2a4a8a; }"
        )

        root = QWidget()
        root.setStyleSheet(f"background: {_DARK};")
        vl = QVBoxLayout(root)
        vl.setContentsMargins(6, 6, 6, 6)
        vl.setSpacing(4)

        # ── 종목 입력 + 버튼 ──────────────────────────────────────
        ctrl_hl = QHBoxLayout()
        ctrl_hl.setSpacing(4)

        code_lbl = QLabel("종목코드:")
        code_lbl.setStyleSheet("color: #aaa; font-size: 11px;")
        ctrl_hl.addWidget(code_lbl)

        self._ovs_code_edit = QLineEdit(self._ovs_code)
        self._ovs_code_edit.setFixedWidth(90)
        self._ovs_code_edit.setStyleSheet(
            "background: #2a2a2a; color: #e0e0e0; border: 1px solid #555;"
            "padding: 2px 4px; font-size: 12px;"
        )
        self._ovs_code_edit.returnPressed.connect(self._on_ovs_refresh)
        ctrl_hl.addWidget(self._ovs_code_edit)

        refresh_btn = QPushButton("조회")
        refresh_btn.setFixedWidth(50)
        refresh_btn.setStyleSheet(
            "background: #334; color: #ccc; border: 1px solid #555;"
            "padding: 2px 6px; font-size: 11px;"
        )
        refresh_btn.clicked.connect(self._on_ovs_refresh)
        ctrl_hl.addWidget(refresh_btn)
        ctrl_hl.addStretch()

        self._ovs_update_lbl = QLabel("대기 중…")
        self._ovs_update_lbl.setStyleSheet("color: #666; font-size: 10px;")
        ctrl_hl.addWidget(self._ovs_update_lbl)
        vl.addLayout(ctrl_hl)

        # ── 현재가 헤더 패널 ─────────────────────────────────────
        price_frame = QFrame()
        price_frame.setStyleSheet(
            "QFrame { background: #252525; border: 1px solid #3a3a3a; border-radius: 4px; }"
        )
        price_hl = QHBoxLayout(price_frame)
        price_hl.setContentsMargins(10, 4, 10, 4)
        price_hl.setSpacing(20)

        def _lbl_pair(title, style="color:#aaa;font-size:10px;",
                      val_style="color:#e0e0e0;font-size:18px;font-weight:bold;"):
            grp = QVBoxLayout()
            grp.setSpacing(0)
            t = QLabel(title); t.setStyleSheet(style)
            v = QLabel("—"); v.setStyleSheet(val_style)
            grp.addWidget(t); grp.addWidget(v)
            return grp, v

        grp1, self._ovs_price_lbl    = _lbl_pair("현재가")
        grp2, self._ovs_change_lbl   = _lbl_pair("등락률")
        grp3, self._ovs_open_lbl     = _lbl_pair("시가",
            val_style="color:#cccccc;font-size:13px;")
        grp4, self._ovs_high_lbl     = _lbl_pair("고가",
            val_style="color:#ff6666;font-size:13px;")
        grp5, self._ovs_low_lbl      = _lbl_pair("저가",
            val_style="color:#6699ff;font-size:13px;")
        grp6, self._ovs_vol_lbl      = _lbl_pair("누적거래량",
            val_style="color:#aaaaaa;font-size:12px;")

        for grp in (grp1, grp2, grp3, grp4, grp5, grp6):
            price_hl.addLayout(grp)
        price_hl.addStretch()
        vl.addWidget(price_frame)

        # ── 분봉 테이블 ──────────────────────────────────────────
        tbl = QTableWidget()
        tbl.setObjectName("ovsFuturesTable")
        tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        tbl.setSelectionBehavior(QTableWidget.SelectRows)
        tbl.setSelectionMode(QTableWidget.SingleSelection)
        tbl.verticalHeader().setVisible(False)
        tbl.setAlternatingRowColors(False)
        tbl.setShowGrid(True)
        tbl.setStyleSheet(_TBL_SS)
        tbl.setColumnCount(6)
        tbl.setHorizontalHeaderLabels(["시간", "시가", "고가", "저가", "종가", "거래량"])
        hdr = tbl.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Stretch)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        tbl.verticalHeader().setDefaultSectionSize(20)
        vl.addWidget(tbl)
        self._ovs_table = tbl

        self.mainTabWidget.addTab(root, "해외선물")

    # ── 자금흐름 모니터 탭 ────────────────────────────────────────

    def _setup_flow_monitor_tab(self) -> None:
        from PyQt5.QtWidgets import QLabel, QPushButton, QVBoxLayout, QHBoxLayout
        from PyQt5.QtCore import Qt

        root   = QWidget()
        vl     = QVBoxLayout(root)
        vl.setContentsMargins(20, 20, 20, 20)
        vl.setSpacing(12)

        title = QLabel("자금흐름 모니터")
        title.setStyleSheet("font-size:16px; font-weight:bold; color:#1a237e;")
        title.setAlignment(Qt.AlignCenter)
        vl.addWidget(title)

        desc = QLabel(
            "섹터 온도계 · 종목별 수급점수 · 시간대별 자금흐름 차트 · 실시간 알림\n"
            "별도 창으로 열려 trading_product 와 실시간 데이터를 공유합니다."
        )
        desc.setAlignment(Qt.AlignCenter)
        desc.setStyleSheet("color:#555; font-size:12px;")
        vl.addWidget(desc)

        self._flow_status_lbl = QLabel("상태: 미실행")
        self._flow_status_lbl.setAlignment(Qt.AlignCenter)
        self._flow_status_lbl.setStyleSheet("color:#888; font-size:11px;")
        vl.addWidget(self._flow_status_lbl)

        hl = QHBoxLayout()
        hl.addStretch()

        open_btn = QPushButton("▶  자금흐름 모니터 열기")
        open_btn.setFixedWidth(200)
        open_btn.setFixedHeight(36)
        open_btn.setStyleSheet(
            "background:#1565c0; color:white; border-radius:5px;"
            "font-size:13px; font-weight:bold;"
        )
        open_btn.clicked.connect(self._open_flow_monitor)
        hl.addWidget(open_btn)

        close_btn = QPushButton("■  닫기")
        close_btn.setFixedWidth(90)
        close_btn.setFixedHeight(36)
        close_btn.setStyleSheet(
            "background:#c62828; color:white; border-radius:5px; font-size:13px;"
        )
        close_btn.clicked.connect(self._close_flow_monitor)
        hl.addWidget(close_btn)

        hl.addStretch()
        vl.addLayout(hl)
        vl.addStretch()

        self.mainTabWidget.addTab(root, "자금흐름")

    def _open_flow_monitor(self) -> None:
        if self._flow_monitor_win is None or not self._flow_monitor_win.isVisible():
            self._flow_monitor_win = FlowMonitor(api=self, db=self.db)

        self._flow_monitor_win.show()
        self._flow_monitor_win.raise_()
        self._flow_monitor_win.activateWindow()
        self._flow_status_lbl.setText("상태: 실행 중")
        logger.info("[FlowMonitor] 자금흐름 모니터 열림")

    def _close_flow_monitor(self) -> None:
        if self._flow_monitor_win and self._flow_monitor_win.isVisible():
            self._flow_monitor_win.close()
        self._flow_status_lbl.setText("상태: 미실행")

    def _on_ovs_refresh(self):
        code = self._ovs_code_edit.text().strip().upper()
        if not code:
            return
        self._ovs_code = code
        self._ovs_candles.clear()
        self._ovs_table.setRowCount(0)
        self._ovs_update_lbl.setText("조회 중…")
        self.tr_req_queue.put([self.request_opt50028, code, "1"])
        self.register_overseas_futures(code)

    @log_exceptions
    def on_receive_overseas_futures_data(self, code: str, df: "pd.DataFrame"):
        if not hasattr(self, '_ovs_table'):
            return
        self._ovs_update_lbl.setText(
            f"{code}  {datetime.datetime.now().strftime('%H:%M:%S')} 갱신 ({len(df)}행)"
        )
        tbl = self._ovs_table
        tbl.setRowCount(0)
        if df.empty:
            return

        UP   = QColor(255,  80,  80)
        DOWN = QColor( 80, 120, 255)
        FLAT = QColor(200, 200, 200)
        BG   = QColor( 30,  30,  30)

        rows = df.to_dict("records")
        tbl.setRowCount(len(rows))
        for r, row in enumerate(rows):
            close = row["Close"]
            open_ = row["Open"]
            color = UP if close > open_ else (DOWN if close < open_ else FLAT)

            def _item(txt, align=Qt.AlignRight | Qt.AlignVCenter, fg=color):
                it = QTableWidgetItem(str(txt))
                it.setTextAlignment(align)
                it.setForeground(fg)
                it.setBackground(BG)
                return it

            date_str = str(row["Date"])
            # 시간 부분만 표시 (YYYYMMDDHHMMSS → HH:MM)
            if len(date_str) >= 12:
                time_part = f"{date_str[8:10]}:{date_str[10:12]}"
            elif len(date_str) >= 6:
                time_part = f"{date_str[0:2]}:{date_str[2:4]}"
            else:
                time_part = date_str

            def _price(v):
                try:
                    f = float(v)
                    return f"{f:,.2f}" if f != int(f) else f"{int(f):,}"
                except Exception:
                    return str(v)

            tbl.setItem(r, 0, _item(time_part, Qt.AlignCenter | Qt.AlignVCenter, FLAT))
            tbl.setItem(r, 1, _item(_price(open_),        fg=FLAT))
            tbl.setItem(r, 2, _item(_price(row["High"]),  fg=UP))
            tbl.setItem(r, 3, _item(_price(row["Low"]),   fg=DOWN))
            tbl.setItem(r, 4, _item(_price(close)))
            tbl.setItem(r, 5, _item(f"{int(row['Volume']):,}", fg=FLAT))

    @log_exceptions
    def on_receive_overseas_futures_realtime(self, code: str, data: dict):
        if not hasattr(self, '_ovs_price_lbl'):
            return
        현재가  = data.get("현재가", 0)
        등락률  = data.get("등락률", 0)
        시가    = data.get("시가", 0)
        고가    = data.get("고가", 0)
        저가    = data.get("저가", 0)
        누적거래량 = data.get("누적거래량", 0)

        def _fmt(v):
            try:
                f = float(v)
                return f"{f:,.2f}" if f != int(f) else f"{int(f):,}"
            except Exception:
                return str(v)

        clr_등락 = "#ff5555" if 등락률 >= 0 else "#5588ff"
        sign = "+" if 등락률 >= 0 else ""

        self._ovs_price_lbl.setText(_fmt(현재가))
        self._ovs_price_lbl.setStyleSheet(
            f"color:{clr_등락};font-size:18px;font-weight:bold;"
        )
        self._ovs_change_lbl.setText(f"{sign}{등락률:.2f}%")
        self._ovs_change_lbl.setStyleSheet(
            f"color:{clr_등락};font-size:14px;font-weight:bold;"
        )
        self._ovs_open_lbl.setText(_fmt(시가))
        self._ovs_high_lbl.setText(_fmt(고가))
        self._ovs_low_lbl.setText(_fmt(저가))
        self._ovs_vol_lbl.setText(f"{누적거래량:,}" if 누적거래량 else "—")

    # ========================
    # 매수 신호 탭
    # ========================

    def _setup_buy_signal_tab(self):
        from PyQt5.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QLabel,
            QTableWidget, QHeaderView, QFrame, QListWidget,
            QPushButton,
        )

        # ── 스타일시트 ──────────────────────────────────────────
        _TBL_SS = (
            "QTableWidget { border: none; }"
            "QTableWidget::item { padding: 3px 6px; }"
            "QHeaderView::section {"
            "  background: #1a1a2e; color: #e0e0e0;"
            "  border: none; border-right: 1px solid #333;"
            "  padding: 4px 6px; font-size: 11px; font-weight: bold; }"
            "QTableWidget::item:selected { background: #264f78; color: white; }"
        )
        _CRITERIA_SS = (
            "QFrame { background: #0f0f1a; border: 1px solid #333;"
            "  border-radius: 4px; }"
        )
        _LOG_SS = (
            "QListWidget { border: none; background: #0f0f1a; color: #ccc; font-size: 11px; }"
            "QListWidget::item { padding: 2px 6px; border-bottom: 1px solid #222; }"
        )

        tab_root = QWidget()
        tab_root.setStyleSheet("background: #12121f;")
        vl = QVBoxLayout(tab_root)
        vl.setContentsMargins(6, 6, 6, 6)
        vl.setSpacing(6)

        # ── 기준 패널 (6개 조건 요약) ────────────────────────────
        crit_frame = QFrame()
        crit_frame.setStyleSheet(_CRITERIA_SS)
        crit_hl = QHBoxLayout(crit_frame)
        crit_hl.setContentsMargins(10, 6, 10, 6)
        crit_hl.setSpacing(18)

        title_lbl = QLabel("[ 매수 기준 ]  강한 섹터의 대장주를 초반 눌림목에서 잡는다")
        title_lbl.setStyleSheet(
            "color: #f0c040; font-size: 12px; font-weight: bold;"
        )
        crit_hl.addWidget(title_lbl)
        crit_hl.addStretch()

        _CRIT_ITEMS = [
            ("C1", CRITERIA_LABELS["C1"], "#ff4444"),
            ("C2", CRITERIA_LABELS["C2"], "#ff8844"),
            ("C3", CRITERIA_LABELS["C3"], "#ffcc44"),
            ("C4", CRITERIA_LABELS["C4"], "#44ff88"),
            ("C5", CRITERIA_LABELS["C5"], "#44aaff"),
            ("C6", CRITERIA_LABELS["C6"], "#cc88ff"),
        ]
        for key, label, color in _CRIT_ITEMS:
            lbl = QLabel(f'<span style="color:{color};font-weight:bold;">{key}</span>'
                         f'<span style="color:#bbb;font-size:10px;"> {label}</span>')
            lbl.setTextFormat(Qt.RichText)
            crit_hl.addWidget(lbl)

        vl.addWidget(crit_frame)

        # ── 컨트롤 바 (조회 버튼 + 범례 + 갱신 시각) ──────────────
        ctrl_hl = QHBoxLayout()
        ctrl_hl.setSpacing(8)

        refresh_btn = QPushButton("⟳  조회")
        refresh_btn.setFixedHeight(26)
        refresh_btn.setFixedWidth(80)
        refresh_btn.setStyleSheet(
            "QPushButton { background: #1e3a5f; color: #7ecfff;"
            "  border: 1px solid #2a5a8f; border-radius: 3px;"
            "  font-size: 12px; font-weight: bold; padding: 0 8px; }"
            "QPushButton:hover { background: #2a4f7a; }"
            "QPushButton:pressed { background: #0f2540; }"
        )
        refresh_btn.clicked.connect(self._on_buy_signal_refresh)
        ctrl_hl.addWidget(refresh_btn)

        legend_lbl = QLabel(
            '<span style="color:#ff2222;font-weight:bold;">★★★</span>'
            '<span style="color:#888;font-size:10px;"> 6개&nbsp;&nbsp;</span>'
            '<span style="color:#ff8800;font-weight:bold;">★★</span>'
            '<span style="color:#888;font-size:10px;"> 5개&nbsp;&nbsp;</span>'
            '<span style="color:#3399ff;font-weight:bold;">★</span>'
            '<span style="color:#888;font-size:10px;"> 4개&nbsp;&nbsp;</span>'
            '<span style="color:#666;font-weight:bold;">⊙</span>'
            '<span style="color:#888;font-size:10px;"> 3개(관심)</span>'
        )
        legend_lbl.setTextFormat(Qt.RichText)
        ctrl_hl.addWidget(legend_lbl)
        ctrl_hl.addStretch()

        self._buy_signal_update_lbl = QLabel("마지막 갱신: —")
        self._buy_signal_update_lbl.setStyleSheet("color: #555; font-size: 10px;")
        ctrl_hl.addWidget(self._buy_signal_update_lbl)

        self._buy_signal_count_lbl = QLabel("")
        self._buy_signal_count_lbl.setStyleSheet(
            "color: #f0c040; font-size: 11px; font-weight: bold; padding-right: 4px;"
        )
        ctrl_hl.addWidget(self._buy_signal_count_lbl)

        vl.addLayout(ctrl_hl)

        # ── 신호 테이블 ──────────────────────────────────────────
        sig_tbl = QTableWidget()
        sig_tbl.setObjectName("buySignalTable")
        sig_tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        sig_tbl.setSelectionBehavior(QTableWidget.SelectRows)
        sig_tbl.setSelectionMode(QTableWidget.SingleSelection)
        sig_tbl.verticalHeader().setVisible(False)
        sig_tbl.setAlternatingRowColors(False)
        sig_tbl.setShowGrid(True)
        sig_tbl.setStyleSheet(_TBL_SS)

        _HEADERS = [
            "신호", "종목명", "섹터",
            "등락률", "섹터순위", "확산도", "외인순매수", "체결강도", "5분속도",
            "C1\n섹터순위", "C2\n확산도", "C3\n대장주",
            "C4\n외인+", "C5\n속도", "C6\n눌림돌파",
        ]
        sig_tbl.setColumnCount(len(_HEADERS))
        sig_tbl.setHorizontalHeaderLabels(_HEADERS)
        hdr = sig_tbl.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)   # 종목명 늘리기
        sig_tbl.verticalHeader().setDefaultSectionSize(26)
        sig_tbl.setRowCount(0)
        vl.addWidget(sig_tbl, stretch=1)
        self._buy_signal_table = sig_tbl

        # ── 하단: 알림 로그 ──────────────────────────────────────
        log_lbl = QLabel("[ 신규 신호 알림 ]")
        log_lbl.setStyleSheet("color: #888; font-size: 10px; padding: 2px 4px;")
        vl.addWidget(log_lbl)

        alert_log = QListWidget()
        alert_log.setObjectName("buySignalLog")
        alert_log.setStyleSheet(_LOG_SS)
        alert_log.setMaximumHeight(90)
        vl.addWidget(alert_log)
        self._buy_signal_log = alert_log

        self.mainTabWidget.addTab(tab_root, "매수 신호 🔔")

    # ========================
    # 섹터 자금 흐름 히스토리 탭
    # ========================

    def _setup_sector_flow_history_tab(self):
        from PyQt5.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QLabel,
            QPushButton, QTableWidget, QHeaderView, QFrame,
        )

        _BG  = "#111"
        _TBL = (
            "QTableWidget{background:#161616;color:#ddd;border:none;"
            "gridline-color:#2a2a2a;}"
            "QTableWidget::item{padding:1px 4px;}"
            "QHeaderView::section{background:#222;color:#999;border:none;"
            "border-bottom:1px solid #333;padding:2px 4px;font-size:10px;}"
            "QTableWidget::item:selected{background:#1a3a6a;}"
        )
        _BTN = ("QPushButton{background:#222;color:#aaa;border:1px solid #444;"
                "font-size:11px;border-radius:3px;padding:1px 4px;}"
                "QPushButton:checked{background:#2255aa;color:#fff;border-color:#3377cc;}")

        root = QWidget(); root.setStyleSheet(f"background:{_BG};")
        vl = QVBoxLayout(root); vl.setContentsMargins(4,4,4,4); vl.setSpacing(3)

        # ── 컨트롤 바 ─────────────────────────────────────────────
        ctrl = QHBoxLayout(); ctrl.setSpacing(4)

        lbl = QLabel("표시:"); lbl.setStyleSheet("color:#888;font-size:11px;")
        ctrl.addWidget(lbl)

        self._flow_btn_f = QPushButton("외인"); self._flow_btn_f.setCheckable(True); self._flow_btn_f.setChecked(True)
        self._flow_btn_i = QPushButton("기관"); self._flow_btn_i.setCheckable(True)
        self._flow_btn_s = QPushButton("합산"); self._flow_btn_s.setCheckable(True)
        for b in (self._flow_btn_f, self._flow_btn_i, self._flow_btn_s):
            b.setFixedSize(50, 22); b.setStyleSheet(_BTN); ctrl.addWidget(b)
        self._flow_btn_f.clicked.connect(lambda: self._on_flow_mode_changed("외인"))
        self._flow_btn_i.clicked.connect(lambda: self._on_flow_mode_changed("기관"))
        self._flow_btn_s.clicked.connect(lambda: self._on_flow_mode_changed("합산"))

        sep_lbl = QLabel("|"); sep_lbl.setStyleSheet("color:#444;"); ctrl.addWidget(sep_lbl)
        lbl2 = QLabel("간격:"); lbl2.setStyleSheet("color:#888;font-size:11px;"); ctrl.addWidget(lbl2)

        self._flow_interval_btns: list = []
        for mins, secs in [(5,300),(10,600),(30,1800)]:
            b = QPushButton(f"{mins}분"); b.setCheckable(True); b.setChecked(secs==600)
            b.setFixedSize(38, 22); b.setStyleSheet(_BTN)
            b.clicked.connect(lambda _, s=secs: self._on_flow_interval_changed(s))
            ctrl.addWidget(b)
            self._flow_interval_btns.append((b, secs))

        sep_lbl2 = QLabel("|"); sep_lbl2.setStyleSheet("color:#444;"); ctrl.addWidget(sep_lbl2)

        # 지금 기록 버튼 (수동 스냅샷)
        snap_now_btn = QPushButton("지금 기록")
        snap_now_btn.setFixedSize(65, 22); snap_now_btn.setStyleSheet(_BTN)
        snap_now_btn.clicked.connect(self._force_flow_snapshot)
        ctrl.addWidget(snap_now_btn)

        # 초기화 버튼
        clear_btn = QPushButton("초기화")
        clear_btn.setFixedSize(50, 22); clear_btn.setStyleSheet(_BTN)
        clear_btn.clicked.connect(self._clear_flow_history)
        ctrl.addWidget(clear_btn)

        sep_lbl3 = QLabel("|"); sep_lbl3.setStyleSheet("color:#444;"); ctrl.addWidget(sep_lbl3)

        # [1051] 장중투자자 조회 버튼
        inv_btn = QPushButton("[1051] 투자자조회")
        inv_btn.setFixedSize(100, 22); inv_btn.setStyleSheet(_BTN)
        inv_btn.clicked.connect(self._start_intraday_investor_query)
        ctrl.addWidget(inv_btn)

        ctrl.addStretch()
        self._flow_ctrl_lbl = QLabel("스냅샷 대기 중 (외인/기관 데이터 로딩 후 자동 기록)")
        self._flow_ctrl_lbl.setStyleSheet("color:#555;font-size:10px;")
        ctrl.addWidget(self._flow_ctrl_lbl)
        vl.addLayout(ctrl)

        # ── 히스토리 테이블 ──────────────────────────────────────
        # 세로 스크롤: 섹터 행 / 가로 스크롤: 시간대 열 (8시~20시 전체)
        hist_tbl = QTableWidget()
        hist_tbl.setObjectName("flowHistTable")
        hist_tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        hist_tbl.setSelectionBehavior(QTableWidget.SelectRows)
        hist_tbl.setSelectionMode(QTableWidget.SingleSelection)
        hist_tbl.verticalHeader().setVisible(False)
        hist_tbl.setAlternatingRowColors(False)
        hist_tbl.setShowGrid(True)
        hist_tbl.setStyleSheet(_TBL)
        hist_tbl.verticalHeader().setDefaultSectionSize(22)
        hist_tbl.setHorizontalScrollMode(QTableWidget.ScrollPerPixel)
        vl.addWidget(hist_tbl, stretch=3)
        self._flow_hist_table = hist_tbl

        # ── 종가 신호 패널 ────────────────────────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#2a2a2a;"); vl.addWidget(sep)

        sig_lbl = QLabel("[ 종가 베팅 신호 — 최근 1시간 외인+기관 순매수 유입 순위 ]")
        sig_lbl.setStyleSheet("color:#888;font-size:10px;")
        vl.addWidget(sig_lbl)

        sig_tbl = QTableWidget()
        sig_tbl.setObjectName("flowSigTable")
        sig_tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        sig_tbl.setSelectionBehavior(QTableWidget.SelectRows)
        sig_tbl.setSelectionMode(QTableWidget.SingleSelection)
        sig_tbl.verticalHeader().setVisible(False)
        sig_tbl.setAlternatingRowColors(False)
        sig_tbl.setShowGrid(False)
        sig_tbl.setStyleSheet(_TBL)
        sig_tbl.setColumnCount(5)
        sig_tbl.setHorizontalHeaderLabels(["순위","섹터","외인(1h)","기관(1h)","대장주"])
        sig_hdr = sig_tbl.horizontalHeader()
        sig_hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        sig_hdr.setSectionResizeMode(4, QHeaderView.Stretch)
        sig_tbl.verticalHeader().setDefaultSectionSize(22)
        sig_tbl.setFixedHeight(185)
        vl.addWidget(sig_tbl, stretch=1)
        self._flow_sig_table = sig_tbl

        # ── [1051] 장중투자자별매매 섹터 집계 패널 ──────────────────
        sep2 = QFrame(); sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet("color:#2a2a2a;"); vl.addWidget(sep2)

        inv_hdr = QHBoxLayout(); inv_hdr.setSpacing(4)
        inv_hdr_lbl = QLabel("[ 장중투자자별매매 [1051] — 섹터별 시간대 집계 (백만원→억원) ]")
        inv_hdr_lbl.setStyleSheet("color:#888;font-size:10px;")
        inv_hdr.addWidget(inv_hdr_lbl)
        inv_hdr.addSpacing(8)

        _INV_BTN = ("QPushButton{background:#1a1a1a;color:#aaa;border:1px solid #333;"
                    "font-size:11px;border-radius:3px;padding:1px 4px;}"
                    "QPushButton:checked{background:#225522;color:#7fff7f;border-color:#336633;}")
        self._inv_btn_f = QPushButton("외인"); self._inv_btn_f.setCheckable(True); self._inv_btn_f.setChecked(True)
        self._inv_btn_i = QPushButton("기관계"); self._inv_btn_i.setCheckable(True)
        self._inv_btn_s = QPushButton("합산"); self._inv_btn_s.setCheckable(True)
        for b in (self._inv_btn_f, self._inv_btn_i, self._inv_btn_s):
            b.setFixedSize(48, 20); b.setStyleSheet(_INV_BTN); inv_hdr.addWidget(b)
        self._inv_btn_f.clicked.connect(lambda: self._on_intraday_mode_changed("외인"))
        self._inv_btn_i.clicked.connect(lambda: self._on_intraday_mode_changed("기관계"))
        self._inv_btn_s.clicked.connect(lambda: self._on_intraday_mode_changed("합산"))

        inv_hdr.addSpacing(8)
        self._intraday_prog_lbl = QLabel("대기 중")
        self._intraday_prog_lbl.setStyleSheet("color:#555;font-size:10px;")
        inv_hdr.addWidget(self._intraday_prog_lbl)
        inv_hdr.addStretch()
        vl.addLayout(inv_hdr)

        intraday_tbl = QTableWidget()
        intraday_tbl.setObjectName("intradayTbl")
        intraday_tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        intraday_tbl.setSelectionBehavior(QTableWidget.SelectRows)
        intraday_tbl.setSelectionMode(QTableWidget.SingleSelection)
        intraday_tbl.verticalHeader().setVisible(False)
        intraday_tbl.setAlternatingRowColors(False)
        intraday_tbl.setShowGrid(True)
        intraday_tbl.setStyleSheet(_TBL)
        intraday_tbl.verticalHeader().setDefaultSectionSize(22)
        intraday_tbl.setFixedHeight(220)
        vl.addWidget(intraday_tbl)
        self._intraday_tbl = intraday_tbl

        self.mainTabWidget.addTab(root, "섹터자금흐름")

    def _on_flow_mode_changed(self, mode: str):
        self._flow_display_mode = mode
        for btn, m in [(self._flow_btn_f,"외인"),(self._flow_btn_i,"기관"),(self._flow_btn_s,"합산")]:
            btn.setChecked(m == mode)
        self._update_flow_history_table()

    def _on_flow_interval_changed(self, secs: int):
        self._flow_snap_interval = secs
        for b, s in self._flow_interval_btns:
            b.setChecked(s == secs)

    def _force_flow_snapshot(self):
        """수동으로 즉시 스냅샷 기록."""
        try:
            summary_df = self.sector_analyzer.get_summary()
            if summary_df.empty:
                return
            t_str = datetime.datetime.now().strftime("%H:%M")
            label = self._flow_schedule.get(t_str, t_str)
            self._do_take_flow_snapshot(summary_df, label)
        except Exception:
            pass

    def _clear_flow_history(self):
        """스냅샷 초기화."""
        self._flow_history.clear()
        self._flow_last_snap_ts = 0.0
        self._flow_done_labels.clear()
        if hasattr(self, '_flow_hist_table'):
            self._flow_hist_table.setRowCount(0)
            self._flow_hist_table.setColumnCount(0)
        if hasattr(self, '_flow_sig_table'):
            self._flow_sig_table.setRowCount(0)
        if hasattr(self, '_flow_ctrl_lbl'):
            self._flow_ctrl_lbl.setText("초기화 완료 — 다음 간격에 자동 기록")

    def _try_take_flow_snapshot(self, summary_df: pd.DataFrame):
        """간격 조건 확인 후 스냅샷 기록."""
        if summary_df.empty:
            return
        now = time.time()
        if now - self._flow_last_snap_ts < self._flow_snap_interval:
            return
        # 외인/기관 데이터 미로드 시 스킵 (opt10059 아직 미수신)
        f_col = "외인순매수합(주)"
        i_col = "기관순매수합(주)"
        f_vals = summary_df[f_col].fillna(0) if f_col in summary_df.columns else pd.Series([0])
        i_vals = summary_df[i_col].fillna(0) if i_col in summary_df.columns else pd.Series([0])
        if (f_vals == 0).all() and (i_vals == 0).all():
            return
        t_str = datetime.datetime.now().strftime("%H:%M")
        label = self._flow_schedule.get(t_str, t_str)
        self._do_take_flow_snapshot(summary_df, label)

    def _check_flow_schedule(self):
        """1분마다 호출 — [1051] 컷오프 스냅샷 + DB 이벤트 처리."""
        t_str = datetime.datetime.now().strftime("%H:%M")

        # 14:30 — 종베 스캐너 자동 팝업
        if t_str == "14:30":
            try:
                from scan_popup import auto_popup_at_1430
                auto_popup_at_1430(trader=self, parent=self)
            except Exception as e:
                logger.warning(f"[종베스캐너] 팝업 실패: {e}")

        # 09:05 — 전일 nxt_data next_open/next_pct 업데이트
        if t_str == "09:05" and not self._nxt_update_done:
            self._nxt_update_done = True
            self._update_nxt_next_day()

        # 20:00 — NXT 장후 수급 저장
        if t_str == "20:00" and not self._nxt_save_done:
            self._nxt_save_done = True
            self._save_nxt_snapshot()

        # 자정 직후 플래그 초기화 (00:01)
        if t_str == "00:01":
            self._nxt_update_done = False
            self._nxt_save_done   = False

        # [1051] 공식 컷오프 스냅샷
        label = self._flow_schedule.get(t_str)
        if not label or label in self._flow_done_labels:
            return
        try:
            summary_df = self.sector_analyzer.get_summary()
        except Exception:
            return
        if summary_df.empty:
            return
        f_col = "외인순매수합(주)"
        i_col = "기관순매수합(주)"
        f_vals = summary_df[f_col].fillna(0) if f_col in summary_df.columns else pd.Series([0])
        i_vals = summary_df[i_col].fillna(0) if i_col in summary_df.columns else pd.Series([0])
        if (f_vals == 0).all() and (i_vals == 0).all():
            return
        self._do_take_flow_snapshot(summary_df, label)
        self._flow_done_labels.add(label)
        logger.info(f"[섹터흐름] {label}({t_str}) 공식 스냅샷 기록")

    def _do_take_flow_snapshot(self, summary_df: pd.DataFrame, label: str = ""):
        """실제 스냅샷 기록."""
        t_str = datetime.datetime.now().strftime("%H:%M")
        display = label if label else t_str
        snap = {"time": t_str, "label": display, "foreign": {}, "inst": {}}
        for _, row in summary_df.iterrows():
            sec = str(row["섹터명"])
            snap["foreign"][sec] = float(row.get("외인순매수합(주)") or 0)
            snap["inst"][sec]    = float(row.get("기관순매수합(주)") or 0)
        self._flow_history.append(snap)
        self._flow_last_snap_ts = time.time()
        # 8시~20시 전체 보관 (720분 ÷ 5분 = 144 + 스케줄 6개 ≤ 200)
        if len(self._flow_history) > 200:
            self._flow_history = self._flow_history[-200:]
        if hasattr(self, '_flow_hist_table'):
            self._update_flow_history_table()
        if hasattr(self, '_flow_ctrl_lbl'):
            self._flow_ctrl_lbl.setText(
                f"마지막 기록: {display}({t_str})  총 {len(self._flow_history)}개"
            )

    def _update_flow_history_table(self):
        if not self._flow_history or not hasattr(self, '_flow_hist_table'):
            return

        mode = self._flow_display_mode
        hist = self._flow_history
        tbl  = self._flow_hist_table

        # 현재 섹터 목록 (가장 최근 스냅샷 기준, 최대 12개)
        sectors = list(hist[-1]["foreign"].keys())[:12]

        # 표시 스냅샷: 전체 (8시~20시 지원 — 가로 스크롤)
        snaps         = hist
        n_time        = len(snaps)
        start_hist_i  = 0   # snaps = hist 전체이므로 offset = 0

        # 컬럼: 섹터(0) + 시간들(1..N) + 합계(N+1)
        tbl.setColumnCount(1 + n_time + 1)
        headers = ["섹터"] + [s.get("label", s["time"]) for s in snaps] + ["합계"]
        tbl.setHorizontalHeaderLabels(headers)

        hdr = tbl.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Fixed)
        tbl.setColumnWidth(0, 120)
        for ci in range(n_time):
            tbl.setColumnWidth(1 + ci, 68)
        tbl.setColumnWidth(1 + n_time, 80)
        # 마지막 열(합계)만 고정, 시간열은 스크롤
        tbl.setRowCount(len(sectors))

        BG = QColor(22, 22, 22)

        def _mode_val(snap, sec):
            if mode == "외인":
                return snap["foreign"].get(sec, 0.0)
            if mode == "기관":
                return snap["inst"].get(sec, 0.0)
            return snap["foreign"].get(sec, 0.0) + snap["inst"].get(sec, 0.0)

        def _delta(ci, sec):
            """i번째 스냅샷의 이전 대비 변화량."""
            hist_i = start_hist_i + ci
            cur    = _mode_val(snaps[ci], sec)
            if hist_i == 0:
                return cur   # 첫 스냅샷은 누적값 그대로
            return cur - _mode_val(hist[hist_i - 1], sec)

        def _fmt_v(v):
            try:
                if abs(v) >= 100_000:
                    return f"{v/10000:+.0f}조"
                if abs(v) >= 10_000:
                    return f"{v/10000:+.1f}조"
                return f"{v:+,.0f}"
            except Exception:
                return str(v)

        def _color_cell(val, ref):
            if not ref:
                ref = 1
            ratio = min(abs(val / ref), 1.0)
            if val > 0:
                r = int(160 + 95 * ratio); g = int(30 * (1 - ratio)); b = int(30 * (1 - ratio))
                return QColor(r, g, b), QColor(max(0, int(40 * ratio) - 10), 0, 0)
            if val < 0:
                r = int(30 * (1 - ratio)); g = int(30 * (1 - ratio)); b = int(160 + 95 * ratio)
                return QColor(r, g, b), QColor(0, 0, max(0, int(40 * ratio) - 10))
            return QColor(100, 100, 100), BG

        # 각 섹터 스케일 (최대 절대값)
        scale = {}
        for sec in sectors:
            vals = [abs(_delta(ci, sec)) for ci in range(n_time)]
            scale[sec] = max(vals) if vals else 1.0

        for r, sec in enumerate(sectors):
            it = QTableWidgetItem(sec)
            it.setForeground(QColor("#5599ff")); it.setBackground(BG)
            it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            tbl.setItem(r, 0, it)

            total = 0.0
            ref   = scale.get(sec, 1.0) or 1.0

            for ci in range(n_time):
                dval  = _delta(ci, sec)
                total += dval
                fg, bg = _color_cell(dval, ref)
                it = QTableWidgetItem(_fmt_v(dval))
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                it.setForeground(fg); it.setBackground(bg)
                tbl.setItem(r, 1 + ci, it)

            # 합계열
            fg_t, bg_t = _color_cell(total, ref * max(n_time, 1))
            it = QTableWidgetItem(_fmt_v(total))
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            it.setForeground(fg_t); it.setBackground(bg_t)
            ft = it.font(); ft.setBold(True); it.setFont(ft)
            tbl.setItem(r, 1 + n_time, it)

        # 마지막 열(합계)로 스크롤
        tbl.scrollToItem(tbl.item(0, 1 + n_time) if tbl.item(0, 1 + n_time) else tbl.item(0, 0))

        self._update_flow_signal_table()

    def _update_flow_signal_table(self):
        if not hasattr(self, '_flow_sig_table') or not self._flow_history:
            return

        sig_tbl  = self._flow_sig_table
        hist     = self._flow_history
        # 최근 1시간 스냅샷 수
        n_recent = max(1, 3600 // max(self._flow_snap_interval, 60))
        recent   = hist[-n_recent:]

        # 섹터별 최근 1시간 델타 합산
        def _delta_sum(sec, key):
            total = 0.0
            for snap in recent:
                hist_i = hist.index(snap)
                cur    = snap[key].get(sec, 0.0)
                if hist_i == 0:
                    total += cur
                else:
                    total += cur - hist[hist_i - 1][key].get(sec, 0.0)
            return total

        all_sectors = set()
        for s in recent:
            all_sectors.update(s["foreign"].keys())

        rows_data = []
        for sec in all_sectors:
            f = _delta_sum(sec, "foreign")
            i = _delta_sum(sec, "inst")
            rows_data.append((sec, f, i))
        rows_data.sort(key=lambda x: x[1] + x[2], reverse=True)
        top = rows_data[:8]

        leader_map: dict[str, str] = {}
        try:
            ld_df = self.sector_analyzer.get_leader_scores()
            for _, lr in ld_df.iterrows():
                leader_map[lr["섹터명"]] = lr["종목명"]
        except Exception:
            pass

        sig_tbl.setRowCount(len(top))
        BG = QColor(22, 22, 22)

        def _fmt_v(v):
            try:
                if abs(v) >= 100_000:
                    return f"{v/10000:+.0f}조"
                if abs(v) >= 10_000:
                    return f"{v/10000:+.1f}조"
                return f"{v:+,.0f}"
            except Exception:
                return str(v)

        for r, (sec, f_val, i_val) in enumerate(top):
            rank_clr = [QColor(220,50,50), QColor(220,130,0), QColor(60,140,220)]
            r_clr    = rank_clr[r] if r < 3 else QColor(180, 180, 180)

            BG2 = QColor(22, 22, 22)

            def _it(txt, fg=QColor(200,200,200), bold=False, align=Qt.AlignCenter, _BG2=BG2):
                item = QTableWidgetItem(str(txt))
                item.setForeground(fg); item.setBackground(_BG2)
                item.setTextAlignment(align)
                if bold:
                    ft = item.font(); ft.setBold(True); item.setFont(ft)
                return item

            sig_tbl.setItem(r, 0, _it(f"{r+1}위", fg=r_clr, bold=(r<3)))
            sig_tbl.setItem(r, 1, _it(sec, fg=QColor("#5599ff"), align=Qt.AlignLeft|Qt.AlignVCenter))
            sig_tbl.setItem(r, 2, _it(_fmt_v(f_val),
                                       fg=QColor(220,60,60) if f_val>0 else QColor(60,80,220),
                                       align=Qt.AlignRight|Qt.AlignVCenter))
            sig_tbl.setItem(r, 3, _it(_fmt_v(i_val),
                                       fg=QColor(220,60,60) if i_val>0 else QColor(60,80,220),
                                       align=Qt.AlignRight|Qt.AlignVCenter))
            sig_tbl.setItem(r, 4, _it(leader_map.get(sec, "—"),
                                       fg=QColor(220,160,0) if r<3 else QColor(160,160,160),
                                       align=Qt.AlignLeft|Qt.AlignVCenter))

    def _on_buy_signal_refresh(self) -> None:
        """매수 신호 조회 버튼 — 거래대금상위 재조회 후 신호 즉시 갱신."""
        # 1) 거래대금상위 TR 재요청 → 수신 후 섹터/점수 업데이트 → 신호 자동 갱신
        self._on_top_trading_refresh()
        # 2) 현재 캐시 기준 신호 즉시 갱신 (TR 응답 오기 전에도 화면 반영)
        self._try_update_buy_signals()

    # ========================
    # [1051] 장중투자자별매매 섹터 집계
    # ========================

    def _on_intraday_mode_changed(self, mode: str):
        self._intraday_inv_mode = mode
        for btn, m in [(self._inv_btn_f,"외인"),(self._inv_btn_i,"기관계"),(self._inv_btn_s,"합산")]:
            btn.setChecked(m == mode)
        self._update_intraday_table()

    def _start_intraday_investor_query(self):
        """거래대금 상위 종목에 대해 opt10063 배치 조회 시작."""
        codes = list(self._top_row_map.keys())
        if not codes:
            if hasattr(self, '_intraday_prog_lbl'):
                self._intraday_prog_lbl.setText("거래대금상위 먼저 조회하세요")
            return
        codes = codes[:100]
        self._intraday_inv_raw.clear()
        self._intraday_sector_agg.clear()
        self._intraday_inv_total = len(codes)
        self._intraday_inv_done  = 0
        if hasattr(self, '_intraday_prog_lbl'):
            self._intraday_prog_lbl.setText(f"조회 중 0/{self._intraday_inv_total}…")
        for code in codes:
            self.tr_req_queue.put([self.request_opt10063, code])
        logger.info(f"[1051] {len(codes)}종목 opt10063 배치 조회 시작")

    def on_receive_intraday_investor(self, code: str, rows: list):
        """opt10063 응답 콜백 — 집계 후 테이블 갱신."""
        self._intraday_inv_raw[code] = rows
        self._intraday_inv_done += 1
        if hasattr(self, '_intraday_prog_lbl'):
            self._intraday_prog_lbl.setText(
                f"조회 중 {self._intraday_inv_done}/{self._intraday_inv_total}…"
            )
        if self._intraday_inv_done >= self._intraday_inv_total and self._intraday_inv_total > 0:
            self._aggregate_intraday_by_sector()
            self._update_intraday_table()
            t_str = datetime.datetime.now().strftime("%H:%M")
            if hasattr(self, '_intraday_prog_lbl'):
                n_with_data = sum(1 for r in self._intraday_inv_raw.values() if r)
                self._intraday_prog_lbl.setText(
                    f"완료 {t_str} — {n_with_data}/{self._intraday_inv_total}종목 수신"
                )

    def _aggregate_intraday_by_sector(self):
        """opt10063 raw 데이터를 섹터 × 시간대로 집계. 단위: 백만원 → 억원(÷100)."""
        agg: dict = {}
        for code, rows in self._intraday_inv_raw.items():
            if not rows:
                continue
            clean_code = code.replace("_AL", "").strip()
            sector = (self.stock_code_to_sector.get(clean_code) or
                      self.stock_code_to_sector.get(code) or "")
            if not sector:
                continue
            for row in rows:
                period = row.get("집계시간", "").strip()
                if not period:
                    continue
                if period not in agg:
                    agg[period] = {}
                if sector not in agg[period]:
                    agg[period][sector] = {"외인": 0.0, "기관계": 0.0}
                agg[period][sector]["외인"]   += row.get("외국인", 0) / 100.0
                agg[period][sector]["기관계"] += row.get("기관계",  0) / 100.0
        self._intraday_sector_agg = agg

    def _update_intraday_table(self):
        """[1051] 섹터별 시간대 집계 테이블 갱신."""
        if not hasattr(self, '_intraday_tbl') or not self._intraday_sector_agg:
            return

        tbl  = self._intraday_tbl
        mode = self._intraday_inv_mode
        agg  = self._intraday_sector_agg

        # 시간대 컬럼 순서 정렬 (_flow_schedule 순서 기준 우선)
        _PERIOD_ORDER = ["1차", "2차", "3차", "4차", "5차"]
        raw_periods = sorted(agg.keys())
        # 이미 "1차"~"5차" 형식이면 그대로, 아니면 raw
        periods = []
        for p in _PERIOD_ORDER:
            if p in raw_periods:
                periods.append(p)
        for p in raw_periods:
            if p not in periods:
                periods.append(p)
        if not periods:
            return

        # 섹터 목록 — 전체 합산 기준 정렬
        def _mode_val(period, sec):
            d = agg.get(period, {}).get(sec, {})
            if mode == "외인":
                return d.get("외인", 0.0)
            if mode == "기관계":
                return d.get("기관계", 0.0)
            return d.get("외인", 0.0) + d.get("기관계", 0.0)

        all_sectors: set = set()
        for p in periods:
            all_sectors.update(agg[p].keys())
        sectors = sorted(all_sectors,
                         key=lambda s: sum(_mode_val(p, s) for p in periods),
                         reverse=True)[:12]

        # 컬럼: 섹터 + 기간들 + 합계
        col_headers = ["섹터"] + periods + ["합계"]
        tbl.setColumnCount(len(col_headers))
        tbl.setHorizontalHeaderLabels(col_headers)
        tbl.setRowCount(len(sectors))

        hdr = tbl.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Fixed)
        tbl.setColumnWidth(0, 120)
        for ci in range(len(periods)):
            tbl.setColumnWidth(1 + ci, 70)
        tbl.setColumnWidth(len(col_headers) - 1, 80)
        hdr.setSectionResizeMode(len(col_headers) - 1, QHeaderView.Stretch)

        BG = QColor(17, 17, 17)

        # 색상 스케일: 전체 값 중 최대 절대값 기준
        all_vals = [abs(_mode_val(p, s)) for s in sectors for p in periods]
        scale_max = max(all_vals) if all_vals else 1.0

        def _color_val(v):
            ratio = min(abs(v) / max(scale_max, 1.0), 1.0)
            if v > 0:
                r = int(160 + 95 * ratio); g = int(30 * (1 - ratio)); b = int(30 * (1 - ratio))
                return QColor(r, g, b)
            if v < 0:
                r = int(30 * (1 - ratio)); g = int(30 * (1 - ratio)); b = int(160 + 95 * ratio)
                return QColor(r, g, b)
            return QColor(100, 100, 100)

        def _fmt(v):
            if abs(v) >= 10000:
                return f"{v/10000:+.1f}조"
            return f"{v:+,.0f}억"

        for r, sec in enumerate(sectors):
            it = QTableWidgetItem(sec)
            it.setForeground(QColor("#5599ff")); it.setBackground(BG)
            it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            tbl.setItem(r, 0, it)

            total = 0.0
            for ci, period in enumerate(periods):
                v = _mode_val(period, sec)
                total += v
                it = QTableWidgetItem(_fmt(v))
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                it.setForeground(_color_val(v)); it.setBackground(BG)
                tbl.setItem(r, 1 + ci, it)

            it = QTableWidgetItem(_fmt(total))
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            it.setForeground(_color_val(total)); it.setBackground(BG)
            ft = it.font(); ft.setBold(True); it.setFont(ft)
            tbl.setItem(r, len(col_headers) - 1, it)

    def _try_update_buy_signals(self) -> None:
        """섹터/점수 데이터를 모아 BuySignalScanner에 전달."""
        try:
            self.buy_signal_scanner.update(
                sector_summary = self.sector_analyzer.get_summary(),
                leader_scores  = self.sector_analyzer.get_leader_scores(),
                velocity_df    = self.sector_analyzer.get_velocity_summary(),
                score_df       = self.stock_scorer.get_scores(),
            )
        except Exception as e:
            logger.debug(f"[매수신호] update 오류: {e}")

    def _on_buy_signal_updated(self, sig_df: pd.DataFrame) -> None:
        """BuySignalScanner 콜백 — 매수 신호 탭 갱신."""
        if hasattr(self, '_dashboard'):
            self._dashboard.on_buy_signal(sig_df)
        if not hasattr(self, '_buy_signal_table'):
            return

        from PyQt5.QtWidgets import QTableWidgetItem, QListWidgetItem

        # ── 갱신 시각·종목 수 레이블 ─────────────────────────────
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        if hasattr(self, '_buy_signal_update_lbl'):
            self._buy_signal_update_lbl.setText(f"마지막 갱신: {now_str}")
        if hasattr(self, '_buy_signal_count_lbl'):
            cnt = len(sig_df) if not sig_df.empty else 0
            strong = len(sig_df[sig_df["signal"].isin(["★★★", "★★"])]) if not sig_df.empty else 0
            if cnt > 0:
                self._buy_signal_count_lbl.setText(
                    f"신호 {cnt}종목  (강신호 {strong})"
                )
            else:
                self._buy_signal_count_lbl.setText("신호 없음")

        # ── 신규 종목 감지 → 알림 로그 ──────────────────────────
        cur_codes = set(sig_df["종목코드"].tolist()) if not sig_df.empty else set()
        new_codes = cur_codes - self._buy_signal_prev_codes
        if new_codes and not sig_df.empty:
            now_str = datetime.datetime.now().strftime("%H:%M:%S")
            for code in new_codes:
                rows = sig_df[sig_df["종목코드"] == code]
                if rows.empty:
                    continue
                row    = rows.iloc[0]
                sig    = row["signal"]
                name   = row["종목명"]
                sector = row["섹터"]
                pct    = row["등락률(%)"]
                msg    = f"{now_str}  {sig}  {name} [{sector}]  {pct:+.1f}%"
                _CLR = {"★★★": QColor(220, 0, 0), "★★": QColor(220, 120, 0), "★": QColor(30, 120, 200), "⊙": QColor(140, 140, 140)}
                item = QListWidgetItem(msg)
                item.setForeground(_CLR.get(sig, QColor(200, 200, 200)))
                if sig == "★★★":
                    f = item.font(); f.setBold(True); f.setPointSize(f.pointSize() + 1)
                    item.setFont(f)
                self._buy_signal_log.insertItem(0, item)
            while self._buy_signal_log.count() > 80:
                self._buy_signal_log.takeItem(self._buy_signal_log.count() - 1)
        self._buy_signal_prev_codes = cur_codes

        # ── 테이블 갱신 ──────────────────────────────────────────
        tbl = self._buy_signal_table

        _CHECK_Y = "✓"
        _CHECK_N = "✗"

        _SIG_COLOR = {
            "★★★": QColor(220, 30,  30),
            "★★":  QColor(220, 130, 0),
            "★":   QColor(50,  130, 220),
            "⊙":   QColor(140, 140, 140),
        }
        _COND_COLS = ["C1", "C2", "C3", "C4", "C5", "C6"]

        if sig_df.empty:
            tbl.setRowCount(0)
            return

        tbl.setRowCount(len(sig_df))
        for r, (_, row) in enumerate(sig_df.iterrows()):
            sig     = row["signal"]
            sig_clr = _SIG_COLOR.get(sig, QColor(200, 200, 200))

            # 행 배경: 신호 강도별 반투명 배경
            if sig == "★★★":
                row_bg = QColor(60, 0, 0)
            elif sig == "★★":
                row_bg = QColor(50, 30, 0)
            elif sig == "★":
                row_bg = QColor(0, 20, 50)
            else:   # ⊙
                row_bg = QColor(25, 25, 35)

            def _cell(text, align=Qt.AlignCenter, fg=None, bg=None, bold=False):
                it = QTableWidgetItem(str(text))
                it.setTextAlignment(align)
                if fg:
                    it.setForeground(fg)
                if bg:
                    it.setBackground(bg)
                else:
                    it.setBackground(row_bg)
                if bold:
                    f = it.font(); f.setBold(True); it.setFont(f)
                return it

            pct      = float(row["등락률(%)"])
            pct_clr  = QColor(255, 80, 80) if pct > 0 else (QColor(80, 120, 255) if pct < 0 else QColor(200, 200, 200))
            fn       = float(row["외인순매수"])
            fn_clr   = QColor(255, 100, 100) if fn > 0 else (QColor(100, 130, 255) if fn < 0 else QColor(160, 160, 160))
            vel      = float(row["5분속도(%)"])
            vel_clr  = QColor(255, 80, 80) if vel >= 100 else (QColor(255, 160, 60) if vel >= 50 else (QColor(100, 200, 100) if vel > 0 else QColor(160, 160, 160)))
            es       = float(row["체결강도(%)"])
            es_clr   = QColor(255, 80, 80) if es >= 150 else (QColor(255, 160, 60) if es >= 120 else QColor(160, 160, 160))

            col_data = [
                # 신호
                (sig, Qt.AlignCenter, sig_clr, None, sig == "★★★"),
                # 종목명
                (row["종목명"], Qt.AlignLeft | Qt.AlignVCenter, QColor(255, 240, 200), None, True),
                # 섹터
                (row["섹터"], Qt.AlignLeft | Qt.AlignVCenter, QColor(160, 210, 255), None, False),
                # 등락률
                (f"{pct:+.2f}%", Qt.AlignRight | Qt.AlignVCenter, pct_clr, None, False),
                # 섹터순위
                (f"{int(row['섹터순위'])}위", Qt.AlignCenter, QColor(220, 220, 100), None, False),
                # 확산도
                (f"{float(row['확산도(%)']):,.0f}%", Qt.AlignCenter,
                 QColor(255, 120, 60) if row["C2"] else QColor(120, 120, 120), None, False),
                # 외인순매수
                (f"{fn:+,.0f}" if (fn != 0 and not math.isnan(fn)) else "—", Qt.AlignRight | Qt.AlignVCenter, fn_clr, None, False),
                # 체결강도
                (f"{es:.0f}%", Qt.AlignRight | Qt.AlignVCenter, es_clr, None, False),
                # 5분속도
                (f"{vel:+.1f}%", Qt.AlignRight | Qt.AlignVCenter, vel_clr, None, False),
            ]
            for c, (txt, align, fg, bg, bold) in enumerate(col_data):
                tbl.setItem(r, c, _cell(txt, align, fg, bg, bold))

            # C1~C6 조건 체크
            for ci, ckey in enumerate(_COND_COLS):
                passed = bool(row[ckey])
                txt  = _CHECK_Y if passed else _CHECK_N
                fg   = QColor(60, 220, 80) if passed else QColor(180, 60, 60)
                bg   = QColor(0, 40, 10) if passed else QColor(40, 0, 0)
                it   = QTableWidgetItem(txt)
                it.setTextAlignment(Qt.AlignCenter)
                it.setForeground(fg)
                it.setBackground(bg)
                if passed:
                    f = it.font(); f.setBold(True); it.setFont(f)
                tbl.setItem(r, 9 + ci, it)

        tbl.resizeRowsToContents()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    trader = CustomAutoTrader()
    sys.exit(app.exec_())
