# -*- coding: utf-8 -*-
"""
자금흐름 모니터 (FlowMonitor)

LEVEL 1 — 업종 온도계   : 외인 + 프로그램 + 거래대금 증가율
LEVEL 2 — 종목별 수급   : 점수화된 실시간 종목 테이블
LEVEL 3 — 시간대별 차트 : 누적 자금흐름 시각화

단독 실행:  python flow_monitor.py
외부 연동:  monitor.feed_data(data_dict)   # trading_product.py 에서 호출
API 연동:   monitor.set_api(kiwoom_api)    # KiwoomAPI 인스턴스 전달
"""
from __future__ import annotations

import os
import sys
import datetime
import random
from typing import Optional

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QListWidget, QListWidgetItem, QComboBox, QSplitter, QFrame,
    QShortcut, QDialog, QGridLayout, QAbstractItemView, QSizePolicy,
    QMessageBox,
)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QSettings
from PyQt5.QtGui import QColor, QFont, QKeySequence, QBrush


# ──────────────────────────────────────────────────────────────
# 차트 라이브러리 선택 — pyqtgraph 우선, 없으면 matplotlib
# ──────────────────────────────────────────────────────────────
_USE_PYQTGRAPH  = False
_USE_MATPLOTLIB = False

try:
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=True, foreground="k")
    _USE_PYQTGRAPH = True
except ImportError:
    try:
        import matplotlib
        matplotlib.use("Qt5Agg")
        from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        import matplotlib.dates as mdates
        _USE_MATPLOTLIB = True
    except ImportError:
        pass


# ──────────────────────────────────────────────────────────────
# 경로 / DBManager 연동
# ──────────────────────────────────────────────────────────────
_CHAPTER_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_CHAPTER_DIR)
if _CHAPTER_DIR not in sys.path:
    sys.path.insert(0, _CHAPTER_DIR)

try:
    from db_manager import DBManager
    _DB_AVAILABLE = True
except ImportError:
    _DB_AVAILABLE = False

_ORG = "StockCoding"
_APP = "FlowMonitor"


# ──────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────
SCORE_GREEN  = 70
SCORE_YELLOW = 50

_C_GREEN       = QColor(210, 245, 210)
_C_BOLD_GREEN  = QColor(155, 225, 155)
_C_YELLOW      = QColor(255, 252, 195)
_C_RED_BG      = QColor(255, 215, 215)
_C_NXT_BG      = QColor(238, 232, 255)
_C_UP          = QColor(185, 30,  30)
_C_DOWN        = QColor(30,  30,  185)
_C_NEUTRAL     = QColor(70,  70,  70)
_C_HEADER_BG   = "#1a237e"

_T_OPEN        = datetime.time( 9,  0)
_T_CLOSE       = datetime.time(15, 30)
_T_NXT_END     = datetime.time(18,  0)


# ──────────────────────────────────────────────────────────────
# 공통 헬퍼
# ──────────────────────────────────────────────────────────────

def get_market_status() -> str:
    t = datetime.datetime.now().time()
    if _T_OPEN  <= t < _T_CLOSE:  return "정규장"
    if _T_CLOSE <= t < _T_NXT_END: return "NXT"
    return "장마감"


def is_market_open() -> bool:
    return get_market_status() in ("정규장", "NXT")


def fmt_money(val: float) -> str:
    """억원 단위 부호 포함 포맷. 0이면 '—'."""
    if val == 0:
        return "—"
    return f"{val:+,.1f}"


def _cell(
    text:  str,
    align: int                = Qt.AlignCenter,
    fg:    Optional[QColor]   = None,
    bg:    Optional[QColor]   = None,
    bold:  bool               = False,
) -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    it.setTextAlignment(align)
    if fg:   it.setForeground(QBrush(fg))
    if bg:   it.setBackground(QBrush(bg))
    if bold:
        f = it.font(); f.setBold(True); it.setFont(f)
    return it


def _hline() -> QFrame:
    sep = QFrame()
    sep.setFrameShape(QFrame.HLine)
    sep.setStyleSheet("color:#ddd;")
    return sep


# ──────────────────────────────────────────────────────────────
# 수급점수 계산
# ──────────────────────────────────────────────────────────────

def calc_score(
    foreigner:     float,
    program:       float,
    volume_ratio:  float,
    consec_buy:    int  = 0,
    prog_accel:    bool = False,
    is_sector_top: bool = False,
) -> int:
    """
    수급점수 (최대 100점)
      외인 순매수 양수     : +20
      외인 연속매수 ≥ 3   : +20
      프로그램 순매수 양수 : +20
      프로그램 가속 중     : +15
      거래대금 비율 ≥ 2.0 : +15
      섹터 1위 대장주      : +10
    """
    s = 0
    if foreigner    >  0:  s += 20
    if consec_buy   >= 3:  s += 20
    if program      >  0:  s += 20
    if prog_accel:         s += 15
    if volume_ratio >= 2.0: s += 15
    if is_sector_top:      s += 10
    return min(s, 100)


def calc_temperature(foreigner: float, program: float, vol_change_pct: float) -> int:
    """
    업종 온도점수 (0~100)
      온도 = (외인 × 0.40 + 프로그램 × 0.35 + 거래대금증가율 × 0.25)
      기준선 50, 300억 = 만점
    """
    REF = 300.0
    fn  = max(-1.0, min(1.0, foreigner    / REF))
    pn  = max(-1.0, min(1.0, program      / REF))
    vn  = max(-1.0, min(1.0, vol_change_pct / 100.0))
    raw = (fn * 0.40 + pn * 0.35 + vn * 0.25) * 100.0
    return int(max(0.0, min(100.0, raw + 50.0)))


# ──────────────────────────────────────────────────────────────
# 데모 데이터 생성기 (API 미연결 시 사용)
# ──────────────────────────────────────────────────────────────

_DEMO_SECTORS = [
    "기계/방산", "전기전자", "반도체", "바이오", "2차전지",
    "철강",      "화학",     "금융",   "건설",   "유통",
]
_DEMO_STOCKS_MAP: dict[str, list[tuple[str, str]]] = {
    "기계/방산":  [("두산로보틱스",    "454910"), ("한화에어로스페이스", "012450")],
    "전기전자":   [("삼성전자",        "005930"), ("LG전자",            "066570")],
    "반도체":     [("SK하이닉스",      "000660"), ("리노공업",           "058470")],
    "바이오":     [("셀트리온",        "068270"), ("삼성바이오로직스",   "207940")],
    "2차전지":    [("LG에너지솔루션",  "373220"), ("POSCO홀딩스",        "005490")],
    "철강":       [("현대제철",        "004020"), ("동국제강",           "001230")],
    "화학":       [("LG화학",          "051910"), ("롯데케미칼",         "011170")],
    "금융":       [("KB금융",          "105560"), ("신한지주",           "055550")],
    "건설":       [("현대건설",        "000720"), ("GS건설",             "006360")],
    "유통":       [("이마트",          "139480"), ("롯데쇼핑",           "023530")],
}


def gen_demo_data() -> dict:
    """API 없을 때 사용하는 현실적 데모 데이터."""
    now     = datetime.datetime.now()
    sectors: dict = {}
    stocks:  dict = {}

    for rank, sec in enumerate(_DEMO_SECTORS, 1):
        fore = round(random.uniform(-80, 260), 1)
        prog = round(random.uniform(-30, 190), 1)
        vol  = round(random.uniform(-40, 110), 1)
        sectors[sec] = {
            "rank":        rank,
            "foreigner":   fore,
            "program":     prog,
            "vol_change":  vol,
            "temperature": calc_temperature(fore, prog, vol),
        }
        for name, code in _DEMO_STOCKS_MAP.get(sec, []):
            f   = round(random.uniform(-20, 110), 1)
            p   = round(random.uniform(-10,  75), 1)
            vr  = round(random.uniform(0.5,  4.2), 2)
            cb  = random.randint(0, 6)
            pa  = random.random() > 0.5
            top = (rank == 1)
            stocks[code] = {
                "name":        name,
                "code":        code,
                "sector":      sec,
                "price":       random.randint(10_000, 900_000),
                "change_pct":  round(random.uniform(-15.0, 15.0), 2),
                "foreigner":   f,
                "program":     p,
                "vol_ratio":   vr,
                "consec_buy":  cb,
                "prog_accel":  pa,
                "is_top":      top,
                "score":       calc_score(f, p, vr, cb, pa, top),
            }
    return {
        "sectors":  sectors,
        "stocks":   stocks,
        "time":     now.strftime("%H%M"),
        "datetime": now,
        "is_demo":  True,
    }


# ──────────────────────────────────────────────────────────────
# FlowDataCollector — 데이터 수집 스레드
# ──────────────────────────────────────────────────────────────

class FlowDataCollector(QThread):
    """
    1분 간격으로 수급 데이터를 수집해 data_updated 시그널을 발행.

    API가 없으면 데모 데이터를 사용.
    실제 KiwoomAPI 연동 시 _collect_from_api() 에 OPT10060 / OPW00001 구현.
    Kiwoom COM 이벤트는 메인 스레드 전용이므로 API 캐시를 읽는 방식으로 설계.
    """
    data_updated = pyqtSignal(dict)

    INTERVAL = 60   # 초

    def __init__(self, api=None, db=None, parent=None) -> None:
        super().__init__(parent)
        self._api     = api
        self._db      = db
        self._running = False
        self._force   = False

    def set_api(self, api) -> None:
        self._api = api

    def force_refresh(self) -> None:
        """즉시 갱신 (F5)."""
        self._force = True

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        self._running = True
        elapsed       = self.INTERVAL   # 시작 즉시 첫 수집

        while self._running:
            if elapsed >= self.INTERVAL or self._force:
                self._force = False
                data = self._collect()
                if data:
                    self.data_updated.emit(data)
                    self._persist(data)
                elapsed = 0
            self.msleep(1_000)
            elapsed += 1

    # ── 수집 ──────────────────────────────────────────────────

    def _collect(self) -> dict:
        if self._api:
            return self._collect_from_api()
        return gen_demo_data()

    def _collect_from_api(self) -> dict:
        """
        실제 TR 연동 스텁.

        OPT10060 — 프로그램매매 종목별 순매수
        OPW00001 — 업종별 외인 순매수
        Kiwoom COM은 메인 스레드에서만 동작하므로
        KiwoomAPI 측에서 캐싱한 데이터를 여기서 읽어 구성.
        """
        # TODO: self._api._program_cache / self._api._foreigner_cache 참조
        return gen_demo_data()

    def _persist(self, data: dict) -> None:
        if not self._db:
            return
        now  = datetime.datetime.now()
        date = now.strftime("%Y%m%d")
        time = now.strftime("%H%M")
        rows = [
            {
                "date":          date,
                "time":          time,
                "code":          code,
                "name":          d.get("name"),
                "sector":        d.get("sector"),
                "foreigner_buy": d.get("foreigner"),
                "program_buy":   d.get("program"),
                "volume":        None,
                "price":         d.get("price"),
                "source":        "regular",
            }
            for code, d in data.get("stocks", {}).items()
        ]
        try:
            self._db.save_supply_batch(rows)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# Section 1: SectorThermometerWidget — 업종 온도계
# ──────────────────────────────────────────────────────────────

class SectorThermometerWidget(QWidget):
    """업종 온도계 테이블 — 순위 / 업종명 / 외인 / 프로그램 / 온도."""

    sector_selected = pyqtSignal(str)

    _COLS   = ["순위", "업종명", "외인(억)", "프로그램(억)", "온도"]
    _WIDTHS = [38,   None,      82,          100,             58]   # None = Stretch

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(2)

        lbl = QLabel("LEVEL 1  업종 온도계")
        lbl.setStyleSheet("font-weight:bold; font-size:13px; padding:3px 2px;")
        vl.addWidget(lbl)

        self._tbl = QTableWidget(0, len(self._COLS))
        self._tbl.setHorizontalHeaderLabels(self._COLS)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._tbl.setAlternatingRowColors(False)
        self._tbl.verticalHeader().setDefaultSectionSize(22)

        hdr = self._tbl.horizontalHeader()
        for i, w in enumerate(self._WIDTHS):
            if w is None:
                hdr.setSectionResizeMode(i, QHeaderView.Stretch)
            else:
                hdr.setSectionResizeMode(i, QHeaderView.Fixed)
                self._tbl.setColumnWidth(i, w)

        self._tbl.cellClicked.connect(self._on_click)
        vl.addWidget(self._tbl)

    def update_data(self, sectors: dict) -> None:
        ranked = sorted(
            sectors.items(),
            key=lambda x: x[1].get("temperature", 0),
            reverse=True,
        )
        self._tbl.setRowCount(len(ranked))

        for r, (sec, d) in enumerate(ranked):
            temp   = d.get("temperature", 0)
            is_top = r < 3
            bg     = _C_BOLD_GREEN if is_top else _temp_to_color(temp)
            fore_v = d.get("foreigner", 0)
            prog_v = d.get("program",  0)

            self._tbl.setItem(r, 0, _cell(str(r + 1), bg=bg, bold=is_top))
            self._tbl.setItem(r, 1, _cell(sec,
                Qt.AlignLeft | Qt.AlignVCenter, bg=bg, bold=is_top))
            self._tbl.setItem(r, 2, _cell(fmt_money(fore_v), bg=bg, bold=is_top,
                fg=_C_UP if fore_v > 0 else _C_DOWN))
            self._tbl.setItem(r, 3, _cell(fmt_money(prog_v), bg=bg, bold=is_top,
                fg=_C_UP if prog_v > 0 else _C_DOWN))
            self._tbl.setItem(r, 4, _cell(f"{temp}점", bg=bg, bold=is_top))

    def _on_click(self, row: int, _col: int) -> None:
        it = self._tbl.item(row, 1)
        if it:
            self.sector_selected.emit(it.text())


def _temp_to_color(temp: int) -> QColor:
    """온도 0~100 → 흰색(낮음) ~ 진초록(높음)."""
    t = max(0, min(100, temp))
    r = max(0, min(255, int(255 - t * 0.80)))
    g = max(0, min(255, int(195 + t * 0.60)))
    b = max(0, min(255, int(195 - t * 0.80)))
    return QColor(r, g, b)


# ──────────────────────────────────────────────────────────────
# Section 2: StockSupplyWidget — 종목별 수급 테이블
# ──────────────────────────────────────────────────────────────

class StockSupplyWidget(QWidget):
    """종목별 수급 테이블 — 순위 / 종목명 / 업종 / 현재가 / 등락률 / 외인 / 프로그램 / 점수."""

    stock_double_clicked = pyqtSignal(dict)

    _COLS = ["순위", "종목명", "업종", "현재가", "등락률(%)", "외인(억)", "프로그램(억)", "점수"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(2)

        lbl = QLabel("LEVEL 2  종목별 수급 추적")
        lbl.setStyleSheet("font-weight:bold; font-size:13px; padding:3px 2px;")
        vl.addWidget(lbl)

        self._no_data = QLabel("데이터 수집 중...")
        self._no_data.setAlignment(Qt.AlignCenter)
        self._no_data.setStyleSheet("color:gray; font-size:14px; padding:20px;")
        vl.addWidget(self._no_data)

        self._tbl = QTableWidget(0, len(self._COLS))
        self._tbl.setHorizontalHeaderLabels(self._COLS)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._tbl.setAlternatingRowColors(False)
        self._tbl.verticalHeader().setDefaultSectionSize(22)
        self._tbl.setVisible(False)

        hdr = self._tbl.horizontalHeader()
        fixed = {0: 36, 2: 80, 3: 78, 4: 68, 5: 78, 6: 95, 7: 46}
        for i, w in fixed.items():
            hdr.setSectionResizeMode(i, QHeaderView.Fixed)
            self._tbl.setColumnWidth(i, w)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)

        self._tbl.doubleClicked.connect(self._on_dbl)
        vl.addWidget(self._tbl)

        self._all:    dict = {}
        self._filter: str  = ""   # 업종 필터

    # ── 공개 API ──────────────────────────────────────────────

    def update_data(self, stocks: dict) -> None:
        self._all = stocks
        self._render()

    def filter_sector(self, sector: str) -> None:
        self._filter = sector
        self._render()

    def clear_filter(self) -> None:
        self._filter = ""
        self._render()

    # ── 내부 ──────────────────────────────────────────────────

    def _render(self) -> None:
        items = [
            (code, d) for code, d in self._all.items()
            if not self._filter or d.get("sector") == self._filter
        ]
        items.sort(key=lambda x: x[1].get("score", 0), reverse=True)

        if not items:
            self._no_data.setVisible(True)
            self._tbl.setVisible(False)
            return

        self._no_data.setVisible(False)
        self._tbl.setVisible(True)
        self._tbl.setRowCount(len(items))

        for r, (code, d) in enumerate(items):
            score = d.get("score", 0)
            chg   = d.get("change_pct", 0.0)
            bg    = (_C_GREEN  if score >= SCORE_GREEN else
                     _C_YELLOW if score >= SCORE_YELLOW else None)
            fg_c  = _C_UP if chg > 0 else (_C_DOWN if chg < 0 else _C_NEUTRAL)

            self._tbl.setItem(r, 0, _cell(str(r + 1), bg=bg))
            self._tbl.setItem(r, 1, _cell(d.get("name", ""),
                Qt.AlignLeft | Qt.AlignVCenter, bg=bg))
            self._tbl.setItem(r, 2, _cell(d.get("sector", ""),
                Qt.AlignLeft | Qt.AlignVCenter, bg=bg))
            self._tbl.setItem(r, 3, _cell(f"{d.get('price', 0):,}", bg=bg))
            self._tbl.setItem(r, 4, _cell(f"{chg:+.2f}%", bg=bg, fg=fg_c))
            self._tbl.setItem(r, 5, _cell(fmt_money(d.get("foreigner", 0)), bg=bg,
                fg=_C_UP if d.get("foreigner", 0) > 0 else _C_DOWN))
            self._tbl.setItem(r, 6, _cell(fmt_money(d.get("program", 0)), bg=bg,
                fg=_C_UP if d.get("program", 0) > 0 else _C_DOWN))
            self._tbl.setItem(r, 7, _cell(
                f"{score}점", bg=bg, bold=(score >= SCORE_GREEN)))

    def _on_dbl(self, idx) -> None:
        row   = idx.row()
        items = [
            (c, d) for c, d in self._all.items()
            if not self._filter or d.get("sector") == self._filter
        ]
        items.sort(key=lambda x: x[1].get("score", 0), reverse=True)
        if 0 <= row < len(items):
            self.stock_double_clicked.emit(items[row][1])


# ──────────────────────────────────────────────────────────────
# Section 3: FlowChartWidget — 시간대별 자금흐름 차트
# ──────────────────────────────────────────────────────────────

class FlowChartWidget(QWidget):
    """5분봉 누적 외인 / 프로그램 라인 차트. NXT 구간 배경 표시."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(2)

        # 타이틀 + 업종 필터 콤보
        hl = QHBoxLayout()
        lbl = QLabel("LEVEL 3  시간대별 자금흐름")
        lbl.setStyleSheet("font-weight:bold; font-size:13px; padding:3px 2px;")
        hl.addWidget(lbl)
        self._combo = QComboBox()
        self._combo.addItem("전체 업종")
        self._combo.setFixedWidth(110)
        self._combo.currentTextChanged.connect(self._on_filter)
        hl.addWidget(self._combo)
        hl.addStretch()
        vl.addLayout(hl)

        # 데이터
        self._times:    list[datetime.datetime] = []
        self._foreign:  list[float] = []
        self._program:  list[float] = []
        self._sec_filter = ""

        # 차트 위젯 초기화
        if _USE_PYQTGRAPH:
            self._build_pyqtgraph(vl)
        elif _USE_MATPLOTLIB:
            self._build_matplotlib(vl)
        else:
            fb = QLabel("차트 라이브러리 없음\npip install pyqtgraph")
            fb.setAlignment(Qt.AlignCenter)
            fb.setStyleSheet("color:gray; font-size:12px;")
            vl.addWidget(fb)

    # ── 초기화 ────────────────────────────────────────────────

    def _build_pyqtgraph(self, vl: QVBoxLayout) -> None:
        date_axis = pg.DateAxisItem(orientation="bottom")
        self._pw  = pg.PlotWidget(
            background="#1c1c1c",
            axisItems={"bottom": date_axis},
        )
        self._pw.setLabel("left",   "누적(억원)", color="#cccccc")
        self._pw.addLegend(offset=(10, 10))
        self._pw.showGrid(x=True, y=True, alpha=0.25)
        self._pw.getAxis("left").setTextPen("#cccccc")

        self._curve_f = self._pw.plot(pen=pg.mkPen("#4488ff", width=2), name="외인")
        self._curve_p = self._pw.plot(pen=pg.mkPen("#44cc44", width=2), name="프로그램")
        self._nxt_rgn = pg.LinearRegionItem(
            [0, 0], brush=pg.mkBrush(150, 100, 255, 40), movable=False
        )
        self._pw.addItem(self._nxt_rgn)
        vl.addWidget(self._pw)

    def _build_matplotlib(self, vl: QVBoxLayout) -> None:
        self._fig    = Figure(figsize=(6, 3.2), facecolor="#1c1c1c")
        self._ax     = self._fig.add_subplot(111, facecolor="#1c1c1c")
        self._canvas = FigureCanvasQTAgg(self._fig)
        vl.addWidget(self._canvas)
        self._redraw_mpl()

    # ── 공개 API ──────────────────────────────────────────────

    def add_datapoint(self, data: dict) -> None:
        """5분마다 호출 — 누적 외인/프로그램 포인트 추가."""
        stocks = data.get("stocks", {})
        if self._sec_filter:
            stocks = {c: d for c, d in stocks.items()
                      if d.get("sector") == self._sec_filter}

        now     = data.get("datetime", datetime.datetime.now())
        total_f = sum(d.get("foreigner", 0) for d in stocks.values())
        total_p = sum(d.get("program",  0) for d in stocks.values())

        self._times.append(now)
        self._foreign.append(total_f)
        self._program.append(total_p)

        # 최대 300포인트 (5분 × 300 = 25시간)
        if len(self._times) > 300:
            self._times   = self._times[-300:]
            self._foreign = self._foreign[-300:]
            self._program = self._program[-300:]

        self._sync_combo(data)
        self.update_chart()

    def update_chart(self) -> None:
        if not self._times:
            return
        if _USE_PYQTGRAPH:
            self._update_pg()
        elif _USE_MATPLOTLIB:
            self._redraw_mpl()

    # ── 내부 ──────────────────────────────────────────────────

    def _update_pg(self) -> None:
        xs = [t.timestamp() for t in self._times]
        self._curve_f.setData(xs, self._foreign)
        self._curve_p.setData(xs, self._program)

        # NXT 구간 (15:30 이후)
        nxt_ts = next(
            (t.timestamp() for t in self._times
             if (t.hour, t.minute) >= (15, 30)),
            None,
        )
        if nxt_ts and xs:
            self._nxt_rgn.setRegion([nxt_ts, xs[-1]])
        else:
            self._nxt_rgn.setRegion([0, 0])

    def _redraw_mpl(self) -> None:
        ax = self._ax
        ax.clear()
        ax.set_facecolor("#1c1c1c")

        if self._times:
            ax.plot(self._times, self._foreign,
                    color="#4488ff", linewidth=1.6, label="외인")
            ax.plot(self._times, self._program,
                    color="#44cc44", linewidth=1.6, label="프로그램")
            ax.axhline(0, color="#666", linewidth=0.8, linestyle="--")

            nxt_t = next(
                (t for t in self._times if (t.hour, t.minute) >= (15, 30)),
                None,
            )
            if nxt_t:
                ax.axvspan(nxt_t, self._times[-1],
                           facecolor="#9664cc", alpha=0.14, label="NXT")

            try:
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
                self._fig.autofmt_xdate(rotation=30, ha="right")
            except Exception:
                pass

            ax.legend(fontsize=8, facecolor="#2a2a2a",
                      edgecolor="#555", labelcolor="white", loc="upper left")

        ax.tick_params(colors="#cccccc", labelsize=7)
        for sp in ax.spines.values():
            sp.set_edgecolor("#444")
        ax.set_ylabel("누적(억원)", color="#cccccc", fontsize=8)
        self._fig.tight_layout(pad=0.5)
        try:
            self._canvas.draw_idle()
        except Exception:
            pass

    def _sync_combo(self, data: dict) -> None:
        secs    = sorted({d.get("sector", "") for d in data.get("stocks", {}).values()
                          if d.get("sector")})
        current = self._combo.currentText()
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem("전체 업종")
        for s in secs:
            self._combo.addItem(s)
        idx = self._combo.findText(current)
        if idx >= 0:
            self._combo.setCurrentIndex(idx)
        self._combo.blockSignals(False)

    def _on_filter(self, text: str) -> None:
        self._sec_filter = "" if text == "전체 업종" else text


# ──────────────────────────────────────────────────────────────
# Section 4: AlertLogWidget — 실시간 알림 로그
# ──────────────────────────────────────────────────────────────

class AlertLogWidget(QWidget):
    """수급 감지 + 스케줄 알림 로그 리스트."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(2)

        hl = QHBoxLayout()
        lbl = QLabel("실시간 알림")
        lbl.setStyleSheet("font-weight:bold; font-size:13px; padding:3px 2px;")
        hl.addWidget(lbl)
        btn_clear = QPushButton("초기화")
        btn_clear.setFixedWidth(50)
        btn_clear.setStyleSheet("font-size:10px; padding:1px 5px;")
        btn_clear.clicked.connect(self._clear)
        hl.addWidget(btn_clear)
        hl.addStretch()
        vl.addLayout(hl)

        self._lst = QListWidget()
        self._lst.setStyleSheet(
            "QListWidget { font-size:11px; border:1px solid #ccc; }"
            "QListWidget::item { padding:2px 5px; }"
        )
        self._lst.setMaximumHeight(200)
        vl.addWidget(self._lst)

        self._prev_score: dict[str, int] = {}   # code → 직전 점수

    def check_and_log(self, stocks: dict) -> None:
        """수급점수 변화 감지 → 자동 로그."""
        ts = datetime.datetime.now().strftime("%H:%M")
        for code, d in stocks.items():
            score = d.get("score", 0)
            prev  = self._prev_score.get(code, 0)
            name  = d.get("name", code)

            if score >= SCORE_GREEN and prev < SCORE_GREEN:
                self._add(
                    f"[{ts}] {name} 수급점수 {score}점 — 종베 후보 등록",
                    level="green",
                )
            elif SCORE_YELLOW <= score < SCORE_GREEN and prev >= SCORE_GREEN:
                self._add(
                    f"[{ts}] {name} 수급 약화 ({prev}→{score}점)",
                    level="yellow",
                )
            elif score < SCORE_YELLOW and prev >= SCORE_GREEN:
                self._add(
                    f"[{ts}] {name} 수급 이탈 ({prev}→{score}점)",
                    level="red",
                )
            self._prev_score[code] = score

    def schedule_log(self, msg: str) -> None:
        """스케줄 이벤트 알림."""
        ts = datetime.datetime.now().strftime("%H:%M")
        self._add(f"[{ts}] ★ {msg}", level="star")

    def _add(self, text: str, level: str = "") -> None:
        it = QListWidgetItem(text)
        if   level == "green":  it.setBackground(QBrush(_C_GREEN))
        elif level == "yellow": it.setBackground(QBrush(_C_YELLOW))
        elif level == "red":    it.setBackground(QBrush(_C_RED_BG))
        elif level == "star":
            it.setForeground(QBrush(QColor(100, 0, 160)))
            f = it.font(); f.setBold(True); it.setFont(f)
        self._lst.insertItem(0, it)
        if self._lst.count() > 300:
            self._lst.takeItem(self._lst.count() - 1)

    def _clear(self) -> None:
        self._lst.clear()
        self._prev_score.clear()


# ──────────────────────────────────────────────────────────────
# StockDetailPopup — 종목 더블클릭 상세 팝업
# ──────────────────────────────────────────────────────────────

class StockDetailPopup(QDialog):
    """종목 수급 상세 정보 팝업."""

    def __init__(self, data: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{data.get('name', '—')}  수급 상세")
        self.setMinimumWidth(300)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        grid = QGridLayout(self)
        grid.setSpacing(8)

        score = data.get("score", 0)
        fields = [
            ("종목명",        data.get("name", "")),
            ("종목코드",      data.get("code", "")),
            ("업종",          data.get("sector", "")),
            ("현재가",        f"{data.get('price', 0):,} 원"),
            ("등락률",        f"{data.get('change_pct', 0):+.2f} %"),
            ("외인 순매수",   f"{data.get('foreigner', 0):+.1f} 억"),
            ("프로그램",      f"{data.get('program',   0):+.1f} 억"),
            ("외인 연속매수", f"{data.get('consec_buy', 0)} 일"),
            ("프로그램 가속", "Y" if data.get("prog_accel") else "N"),
            ("거래량 비율",   f"{data.get('vol_ratio', 0):.2f} ×"),
            ("수급점수",      f"{score} 점"),
        ]
        for r, (k, v) in enumerate(fields):
            key_lbl = QLabel(k + ":")
            key_lbl.setStyleSheet("font-weight:bold;")
            val_lbl = QLabel(str(v))
            if k == "등락률":
                c = data.get("change_pct", 0)
                val_lbl.setStyleSheet(
                    "color:red;" if c > 0 else ("color:blue;" if c < 0 else "")
                )
            if k == "수급점수":
                val_lbl.setStyleSheet(
                    "color:#1b5e20; font-weight:bold;" if score >= SCORE_GREEN
                    else ("color:#f57f17;" if score >= SCORE_YELLOW else "")
                )
            grid.addWidget(key_lbl, r, 0)
            grid.addWidget(val_lbl, r, 1)

        btn = QPushButton("닫기")
        btn.clicked.connect(self.accept)
        grid.addWidget(btn, len(fields), 0, 1, 2)


# ──────────────────────────────────────────────────────────────
# FlowMonitor — 메인 QMainWindow
# ──────────────────────────────────────────────────────────────

class FlowMonitor(QMainWindow):
    """
    자금흐름 모니터 메인 창.

    단독:    FlowMonitor().show()
    연동:    FlowMonitor(api=kiwoom_api_instance)
    데이터 주입: monitor.feed_data(data_dict)
    """

    def __init__(self, api=None, db=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("자금흐름 모니터")
        self.setMinimumSize(1120, 700)

        self._settings     = QSettings(_ORG, _APP)
        self._auto_refresh = True
        self._chart_tick   = 0   # 5분 카운터 (분봉 단위)

        # DB — 외부에서 주입된 DBManager 우선 사용, 없으면 자체 생성
        self._db: Optional["DBManager"] = db
        if self._db is None and _DB_AVAILABLE:
            try:
                self._db = DBManager()
            except Exception:
                pass

        # UI
        self._build_ui()
        self._setup_timers()
        QShortcut(QKeySequence("F5"), self, activated=self._force_refresh)

        # 설정 복원
        self.load_settings()

        # 수집 스레드
        self._collector = FlowDataCollector(api=api, db=self._db)
        self._collector.data_updated.connect(self._on_data_updated)
        self._collector.start()

        # 오늘 DB 데이터 복원
        self._restore_from_db()

    # ── UI 구성 ────────────────────────────────────────────────

    def _build_ui(self) -> None:
        center = QWidget()
        self.setCentralWidget(center)
        root = QVBoxLayout(center)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        root.addWidget(self._build_header())

        # 메인 스플리터
        main_split = QSplitter(Qt.Horizontal)

        # ── 좌측 패널 (섹터 + 종목)
        left = QWidget()
        lv   = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(4)

        # 마켓 필터 + 업종 클리어 버튼
        hl_filter = QHBoxLayout()
        hl_filter.addWidget(QLabel("시장:"))
        self._mkt_combo = QComboBox()
        self._mkt_combo.addItems(["전체", "코스피", "코스닥"])
        self._mkt_combo.setFixedWidth(88)
        self._mkt_combo.currentTextChanged.connect(self._on_market_filter)
        hl_filter.addWidget(self._mkt_combo)
        self._clear_sec_btn = QPushButton("업종 전체")
        self._clear_sec_btn.setFixedWidth(72)
        self._clear_sec_btn.setStyleSheet("font-size:10px; padding:2px 4px;")
        self._clear_sec_btn.clicked.connect(self._clear_sector_filter)
        hl_filter.addWidget(self._clear_sec_btn)
        hl_filter.addStretch()
        lv.addLayout(hl_filter)

        # 수직 스플리터: 섹터(위) + 종목(아래)
        left_split = QSplitter(Qt.Vertical)

        self._sector_wgt = SectorThermometerWidget()
        self._sector_wgt.sector_selected.connect(self._on_sector_selected)
        left_split.addWidget(self._sector_wgt)

        self._stock_wgt = StockSupplyWidget()
        self._stock_wgt.stock_double_clicked.connect(self._on_stock_dbl)
        left_split.addWidget(self._stock_wgt)

        left_split.setSizes([220, 400])
        lv.addWidget(left_split)
        main_split.addWidget(left)

        # ── 우측 패널 (차트 + 알림)
        right  = QWidget()
        rv     = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(4)

        right_split = QSplitter(Qt.Vertical)

        self._chart_wgt = FlowChartWidget()
        right_split.addWidget(self._chart_wgt)

        self._alert_wgt = AlertLogWidget()
        right_split.addWidget(self._alert_wgt)

        right_split.setSizes([420, 200])
        rv.addWidget(right_split)
        main_split.addWidget(right)

        main_split.setSizes([490, 630])
        root.addWidget(main_split)

        self.statusBar().showMessage("초기화 완료 — 데이터 수집 대기 중")

    def _build_header(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(44)
        bar.setStyleSheet(f"background:{_C_HEADER_BG}; border-radius:4px;")
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(12, 4, 12, 4)
        hl.setSpacing(10)

        title = QLabel("자금흐름 모니터")
        title.setStyleSheet("color:white; font-size:16px; font-weight:bold;")
        hl.addWidget(title)
        hl.addStretch()

        self._clock_lbl = QLabel("--:--:--")
        self._clock_lbl.setStyleSheet(
            "color:#bbdefb; font-size:14px; font-family:monospace;"
        )
        hl.addWidget(self._clock_lbl)

        self._mkt_lbl = QLabel("장마감")
        self._mkt_lbl.setFixedWidth(58)
        self._mkt_lbl.setAlignment(Qt.AlignCenter)
        self._mkt_lbl.setStyleSheet(
            "background:#37474f; color:white; border-radius:3px;"
            "font-size:12px; padding:2px 5px;"
        )
        hl.addWidget(self._mkt_lbl)

        self._auto_btn = QPushButton("자동갱신 ON")
        self._auto_btn.setCheckable(True)
        self._auto_btn.setChecked(True)
        self._auto_btn.setFixedWidth(90)
        self._auto_btn.setStyleSheet(
            "QPushButton         { background:#2e7d32; color:white; border-radius:3px;"
            "                      font-size:11px; padding:3px 8px; }"
            "QPushButton:!checked{ background:#b71c1c; }"
        )
        self._auto_btn.toggled.connect(self._on_auto_toggle)
        hl.addWidget(self._auto_btn)

        refresh_btn = QPushButton("수동갱신(F5)")
        refresh_btn.setFixedWidth(90)
        refresh_btn.setStyleSheet(
            "background:#0d47a1; color:white; border-radius:3px;"
            "font-size:11px; padding:3px 8px;"
        )
        refresh_btn.clicked.connect(self._force_refresh)
        hl.addWidget(refresh_btn)

        return bar

    # ── 타이머 ─────────────────────────────────────────────────

    def _setup_timers(self) -> None:
        # 1초: 시계 + 장상태
        t1 = QTimer(self)
        t1.timeout.connect(self._tick_clock)
        t1.start(1_000)

        # 1분: 스케줄 체크 (14:30 / 20:00)
        t2 = QTimer(self)
        t2.timeout.connect(self._tick_schedule)
        t2.start(60_000)

        # 5분: 차트 포인트 추가
        t3 = QTimer(self)
        t3.timeout.connect(self._tick_chart)
        t3.start(300_000)

    # ── 타이머 핸들러 ──────────────────────────────────────────

    def _tick_clock(self) -> None:
        now    = datetime.datetime.now()
        status = get_market_status()
        self._clock_lbl.setText(now.strftime("%H:%M:%S"))
        self._mkt_lbl.setText(status)
        color = {"정규장": "#1b5e20", "NXT": "#4a148c"}.get(status, "#37474f")
        self._mkt_lbl.setStyleSheet(
            f"background:{color}; color:white; border-radius:3px;"
            "font-size:12px; padding:2px 5px;"
        )

    def _tick_schedule(self) -> None:
        t = datetime.datetime.now().strftime("%H:%M")
        if t == "14:30":
            self._alert_wgt.schedule_log(
                "14:30 종베 스캐너 자동 실행 — 후보종목 팝업 확인"
            )
            QMessageBox.information(
                self, "스케줄 알림",
                "14:30 — 종베 스캐너 실행 시각입니다.\n수급점수 70점 이상 후보를 확인하세요.",
            )
        elif t == "20:00":
            self._alert_wgt.schedule_log(
                "20:00 NXT 최종 수급 확정 — 보유/청산 자동판단 시작"
            )
            QMessageBox.information(
                self, "스케줄 알림",
                "20:00 — NXT 수급 수집을 시작합니다.\nNXT 수급 확정 후 보유/청산을 판단하세요.",
            )

    def _tick_chart(self) -> None:
        """5분마다 차트에 최신 데이터 포인트 추가."""
        if hasattr(self, "_last_data") and self._last_data:
            self._chart_wgt.add_datapoint(self._last_data)

    # ── 이벤트 핸들러 ──────────────────────────────────────────

    def _on_data_updated(self, data: dict) -> None:
        if not self._auto_refresh:
            return
        self._last_data = data
        self._sector_wgt.update_data(data.get("sectors", {}))
        self._stock_wgt.update_data(data.get("stocks",  {}))
        self._alert_wgt.check_and_log(data.get("stocks", {}))
        tag = "데모" if data.get("is_demo") else "실시간"
        self.statusBar().showMessage(
            f"갱신: {datetime.datetime.now():%H:%M:%S}  |  "
            f"종목 {len(data.get('stocks', {}))}개  |  {tag}"
        )

    def feed_data(self, data: dict) -> None:
        """외부(trading_product.py)에서 직접 데이터 주입."""
        self._on_data_updated(data)

    def set_api(self, api) -> None:
        """KiwoomAPI 인스턴스 설정 — 실시간 데이터 전환."""
        self._collector.set_api(api)

    def _on_sector_selected(self, sector: str) -> None:
        self._stock_wgt.filter_sector(sector)
        self.statusBar().showMessage(f"업종 필터 적용: {sector}")

    def _clear_sector_filter(self) -> None:
        self._stock_wgt.clear_filter()
        self.statusBar().showMessage("업종 필터 해제")

    def _on_stock_dbl(self, data: dict) -> None:
        StockDetailPopup(data, parent=self).exec_()

    def _on_auto_toggle(self, checked: bool) -> None:
        self._auto_refresh = checked
        self._auto_btn.setText("자동갱신 ON" if checked else "자동갱신 OFF")

    def _on_market_filter(self, text: str) -> None:
        # 코스피/코스닥 구분 데이터가 추가되면 이 핸들러에서 필터링
        self.statusBar().showMessage(f"시장 필터: {text}")

    def _force_refresh(self) -> None:
        self._collector.force_refresh()
        self.statusBar().showMessage("수동 갱신 요청…")

    # ── DB 복원 ────────────────────────────────────────────────

    def _restore_from_db(self) -> None:
        if not self._db:
            return
        try:
            holdings = self._db.get_holding_list()
            if holdings:
                self._alert_wgt.schedule_log(
                    f"DB 복원 완료 — 보유종목 {len(holdings)}개"
                )
            self.statusBar().showMessage(
                f"[DB] 보유종목 {len(holdings)}개 복원 완료"
            )
        except Exception as e:
            self.statusBar().showMessage(f"[DB] 복원 실패: {e}")

    # ── 설정 저장 / 불러오기 ───────────────────────────────────

    def save_settings(self) -> None:
        s = self._settings
        s.setValue("geometry",     self.saveGeometry())
        s.setValue("windowState",  self.saveState())
        s.setValue("autoRefresh",  self._auto_refresh)
        s.setValue("marketFilter", self._mkt_combo.currentIndex())
        s.setValue("db_path",
                   self._db.db_path if self._db else "")

    def load_settings(self) -> None:
        s = self._settings
        geo = s.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        state = s.value("windowState")
        if state:
            self.restoreState(state)
        self._auto_refresh = s.value("autoRefresh", True, bool)
        self._auto_btn.setChecked(self._auto_refresh)
        idx = s.value("marketFilter", 0, int)
        self._mkt_combo.setCurrentIndex(idx)

    # ── 종료 ───────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self.save_settings()
        self._collector.stop()
        self._collector.wait(2_000)
        if self._db:
            self._db.close()
        super().closeEvent(event)


# ──────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    monitor = FlowMonitor()
    monitor.show()
    sys.exit(app.exec_())
    sys.exit(app.exec_())
