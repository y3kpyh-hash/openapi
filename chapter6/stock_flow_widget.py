# -*- coding: utf-8 -*-
"""
stock_flow_widget.py — 종목별 수급 흐름 히스토리 위젯

테이블 구조:
  고정 컬럼: 종목명 | 섹터 | 구분(외인/기관/금투)
  시간 컬럼: DB에 실제 저장된 시각(분 단위, 동적)

색상 규칙:
  양수(매수우세)   → 빨강 계열
  음수(매도우세)   → 파랑 계열
  부호 전환 셀     → 굵게 강조
  현재 시각 컬럼  → 연한 노란 배경
  외인/기관/금투 행 → 종목별 묶음 배경색 교대
"""
from __future__ import annotations

import datetime
import math
from typing import Optional

import pandas as pd

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QBrush
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QSizePolicy, QMessageBox,
    QDoubleSpinBox,
)

try:
    from stock_flow_db import StockFlowDB
except ImportError:
    from chapter6.stock_flow_db import StockFlowDB

# ── 색상 팔레트 ──────────────────────────────────────────────

_POS_STRONG  = QColor(0xC6, 0x28, 0x28)
_POS_MID     = QColor(0xE5, 0x73, 0x73)
_POS_WEAK    = QColor(0xFF, 0xCD, 0xCD)
_NEG_STRONG  = QColor(0x0D, 0x47, 0xA1)
_NEG_MID     = QColor(0x42, 0x7B, 0xC5)
_NEG_WEAK    = QColor(0xBB, 0xCF, 0xEA)
_ZERO_BG     = QColor(0xF5, 0xF5, 0xF5)
_CUR_COL_BG  = QColor(0xFF, 0xFF, 0xCC)
_TEXT_LIGHT  = QColor(Qt.white)
_TEXT_DARK   = QColor(Qt.black)
_GROUP_BG    = [QColor(0xFF, 0xFF, 0xFF), QColor(0xF2, 0xF2, 0xF2)]
_INV_LABEL_COLOR = {
    "프로그램": QColor(0xC6, 0x28, 0x28),   # 프로그램순매수 (opt90013)
    "기관":     QColor(0x1B, 0x5E, 0x20),
    "금투":     QColor(0x4A, 0x14, 0x8C),
}

# 고정 컬럼
_FIXED_COLS  = ["종목명", "섹터", "구분", "현재가", "전일대비", "등락률(%)", "시가총액(억)"]
_N_FIXED     = len(_FIXED_COLS)
_N_PRICE_COL = 4   # 고정 컬럼 중 가격 관련 컬럼 수


def _net_bg(val: float, abs_ref: float) -> QColor:
    if math.isnan(val) or val == 0:
        return _ZERO_BG
    ratio = abs(val) / max(abs_ref, 1.0)
    if val > 0:
        if ratio >= 0.6: return _POS_STRONG
        if ratio >= 0.2: return _POS_MID
        return _POS_WEAK
    else:
        if ratio >= 0.6: return _NEG_STRONG
        if ratio >= 0.2: return _NEG_MID
        return _NEG_WEAK


def _text_color(bg: QColor) -> QColor:
    lum = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
    return _TEXT_LIGHT if lum < 140 else _TEXT_DARK


def _current_slot() -> str:
    """현재 시각의 1분 버킷 슬롯 문자열 (예: '14:17')."""
    now = datetime.datetime.now()
    return f"{now.hour:02d}:{now.minute:02d}"


# ─────────────────────────────────────────────────────────────
# StockFlowWidget
# ─────────────────────────────────────────────────────────────

class StockFlowWidget(QWidget):
    """
    종목별 수급 흐름 히스토리 테이블 위젯.

    시간 컬럼은 DB에 실제 저장된 시각(분 단위)으로 동적 생성.
    배치가 2분마다 완료되면 컬럼이 자동으로 추가됨.
    """

    def __init__(self, db: Optional[StockFlowDB] = None, parent=None) -> None:
        super().__init__(parent)
        self._db         = db or StockFlowDB()
        self._df         = pd.DataFrame()
        self._time_slots: list[str] = []   # 현재 렌더링된 시간 컬럼 목록
        self._date       = ""
        self._sector_filter = ""
        self._live_prices: dict = {}       # {code: {현재가,전일대비,등락률,시가총액}}
        self._row_info: list   = []        # [(code, inv_type), ...] — 행 인덱스 매핑
        self._syncing_scroll   = False     # 수직 스크롤 동기화 재진입 방지

        # 수급Δ → 자동매매현황 연계 설정값 (trading_product가 읽어 감)
        self._delta_enabled:   bool  = False  # Δ신호 활성 여부
        self._delta_mode:      int   = 0      # 0=프로그램Δ절대 1=급증배율 2=동시유입
        self._delta_mode_exec: int   = 0      # 0=반자동 1=자동
        self._delta_prog_min:  float = 1000.0 # 임계값 (억원 단위)
        self._delta_surge_x:   float = 3.0    # 급증 배율
        self._delta_inst_w:    float = 0.3    # 기관 가중치

        self._build_ui()

        # 60초마다 DB에서 최신 데이터 자동 로드 (배치 체인 누락 방어)
        self._refresh_timer = QTimer()
        self._refresh_timer.timeout.connect(self._auto_refresh)
        self._refresh_timer.start(60_000)

    # ── UI 빌드 ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        vl = QVBoxLayout(self)
        vl.setContentsMargins(4, 4, 4, 4)
        vl.setSpacing(4)

        # ── 툴바 ─────────────────────────────────────────────
        tool = QHBoxLayout()

        title = QLabel("종목별 수급 흐름 (Δ2분 증감 — 프로그램/기관/금투)")
        title.setStyleSheet("font-weight:bold; font-size:13px;")
        tool.addWidget(title)
        tool.addStretch()

        tool.addWidget(QLabel("날짜:"))
        self._date_combo = QComboBox()
        self._date_combo.setFixedWidth(110)
        self._date_combo.currentTextChanged.connect(self._on_date_changed)
        tool.addWidget(self._date_combo)

        tool.addWidget(QLabel("섹터:"))
        self._sector_combo = QComboBox()
        self._sector_combo.setFixedWidth(120)
        self._sector_combo.addItem("전체")
        self._sector_combo.currentTextChanged.connect(self._on_sector_changed)
        tool.addWidget(self._sector_combo)

        self._last_lbl = QLabel("마지막 스냅샷: —")
        self._last_lbl.setStyleSheet("color:#888; font-size:11px;")
        tool.addWidget(self._last_lbl)

        reload_btn = QPushButton("🔄 새로고침")
        reload_btn.setFixedWidth(90)
        reload_btn.clicked.connect(lambda: self.load_today(self._date))
        tool.addWidget(reload_btn)

        reset_btn = QPushButton("🗑 오늘 초기화")
        reset_btn.setFixedWidth(95)
        reset_btn.setStyleSheet(
            "QPushButton{background:#5a1a1a;color:#ffaaaa;border:1px solid #a04040;"
            "border-radius:3px;font-size:10px;padding:2px 4px;}"
            "QPushButton:hover{background:#7a2a2a;}"
        )
        reset_btn.clicked.connect(self._on_reset_today)
        tool.addWidget(reset_btn)

        vl.addLayout(tool)

        # ── 수급Δ → 자동매매현황 연계 툴바 ─────────────────────
        delta_bar = QHBoxLayout()
        delta_bar.setSpacing(6)

        delta_bar.addWidget(QLabel("▶ Δ연계:"))

        self._delta_mode_combo = QComboBox()
        self._delta_mode_combo.setFixedWidth(145)
        self._delta_mode_combo.addItems([
            "① 프로그램Δ 절대값",
            "② 급증 감지(배율)",
            "③ 동시유입(프로+기관)",
        ])
        self._delta_mode_combo.currentIndexChanged.connect(self._on_delta_mode_changed)
        delta_bar.addWidget(self._delta_mode_combo)

        delta_bar.addWidget(QLabel("임계(억):"))
        self._delta_thresh_spn = QDoubleSpinBox()
        self._delta_thresh_spn.setRange(100, 50000)
        self._delta_thresh_spn.setSingleStep(100)
        self._delta_thresh_spn.setValue(1000)
        self._delta_thresh_spn.setFixedWidth(75)
        self._delta_thresh_spn.valueChanged.connect(
            lambda v: setattr(self, '_delta_prog_min', v))
        delta_bar.addWidget(self._delta_thresh_spn)

        # mode=1 전용: 배율 스핀박스
        self._delta_surge_lbl = QLabel("배율:")
        self._delta_surge_spn = QDoubleSpinBox()
        self._delta_surge_spn.setRange(1.0, 20.0)
        self._delta_surge_spn.setSingleStep(0.5)
        self._delta_surge_spn.setValue(3.0)
        self._delta_surge_spn.setFixedWidth(58)
        self._delta_surge_spn.valueChanged.connect(
            lambda v: setattr(self, '_delta_surge_x', v))
        delta_bar.addWidget(self._delta_surge_lbl)
        delta_bar.addWidget(self._delta_surge_spn)

        # mode=2 전용: 기관 가중치 스핀박스
        self._delta_instw_lbl = QLabel("기관가중치:")
        self._delta_instw_spn = QDoubleSpinBox()
        self._delta_instw_spn.setRange(0.0, 1.0)
        self._delta_instw_spn.setSingleStep(0.05)
        self._delta_instw_spn.setValue(0.3)
        self._delta_instw_spn.setFixedWidth(58)
        self._delta_instw_spn.valueChanged.connect(
            lambda v: setattr(self, '_delta_inst_w', v))
        delta_bar.addWidget(self._delta_instw_lbl)
        delta_bar.addWidget(self._delta_instw_spn)

        # OFF / 반자동 / 자동 버튼
        _btn_base = ("border-radius:3px; font-size:11px; padding:2px 6px;"
                     "border:1px solid #888;")
        self._delta_btn_off   = QPushButton("● OFF")
        self._delta_btn_semi  = QPushButton("🔔 반자동")
        self._delta_btn_auto  = QPushButton("⚡ 자동")
        for btn in (self._delta_btn_off, self._delta_btn_semi, self._delta_btn_auto):
            btn.setFixedWidth(70)
            btn.setStyleSheet(_btn_base + "background:#555; color:#ccc;")
        self._delta_btn_off.clicked.connect(lambda: self._set_delta_exec(None))
        self._delta_btn_semi.clicked.connect(lambda: self._set_delta_exec(0))
        self._delta_btn_auto.clicked.connect(lambda: self._set_delta_exec(1))
        delta_bar.addWidget(self._delta_btn_off)
        delta_bar.addWidget(self._delta_btn_semi)
        delta_bar.addWidget(self._delta_btn_auto)

        self._delta_status_lbl = QLabel("● 대기")
        self._delta_status_lbl.setStyleSheet("color:#888; font-size:11px;")
        delta_bar.addWidget(self._delta_status_lbl)

        delta_bar.addStretch()
        vl.addLayout(delta_bar)

        # 초기 표시 상태
        self._on_delta_mode_changed(0)

        # ── 틀고정 레이아웃: 고정 테이블 + 시간 테이블 ──────
        table_hl = QHBoxLayout()
        table_hl.setSpacing(0)
        table_hl.setContentsMargins(0, 0, 0, 0)

        # 고정 테이블 (종목명~시가총액, 7 컬럼 — 수평 스크롤 없음)
        self._fixed_table = QTableWidget(0, _N_FIXED)
        self._fixed_table.setHorizontalHeaderLabels(_FIXED_COLS)
        self._fixed_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._fixed_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._fixed_table.setAlternatingRowColors(False)
        self._fixed_table.verticalHeader().hide()
        self._fixed_table.setStyleSheet("font-size:11px;")
        self._fixed_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._fixed_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._fixed_table.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        hdr_f = self._fixed_table.horizontalHeader()
        hdr_f.setSectionResizeMode(0, QHeaderView.ResizeToContents)  # 종목명
        hdr_f.setSectionResizeMode(1, QHeaderView.ResizeToContents)  # 섹터
        hdr_f.setSectionResizeMode(2, QHeaderView.Fixed)             # 구분
        self._fixed_table.setColumnWidth(2, 68)
        hdr_f.setSectionResizeMode(3, QHeaderView.Fixed)             # 현재가
        self._fixed_table.setColumnWidth(3, 72)
        hdr_f.setSectionResizeMode(4, QHeaderView.Fixed)             # 전일대비
        self._fixed_table.setColumnWidth(4, 62)
        hdr_f.setSectionResizeMode(5, QHeaderView.Fixed)             # 등락률
        self._fixed_table.setColumnWidth(5, 58)
        hdr_f.setSectionResizeMode(6, QHeaderView.Fixed)             # 시가총액
        self._fixed_table.setColumnWidth(6, 72)
        table_hl.addWidget(self._fixed_table)

        # 시간 테이블 (수평 스크롤 가능, 시간 컬럼만)
        self._table = QTableWidget(0, 0)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setAlternatingRowColors(False)
        self._table.verticalHeader().hide()
        self._table.setStyleSheet("font-size:11px;")
        self._table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        table_hl.addWidget(self._table)

        vl.addLayout(table_hl)

        # 수직 스크롤 동기화
        self._table.verticalScrollBar().valueChanged.connect(self._on_time_vscroll)
        self._fixed_table.verticalScrollBar().valueChanged.connect(self._on_fixed_vscroll)

        # ── 범례 ─────────────────────────────────────────────
        legend = QHBoxLayout()
        legend.setSpacing(12)

        def _chip(color: QColor, text: str) -> QLabel:
            lbl = QLabel(f"  {text}  ")
            r, g, b = color.red(), color.green(), color.blue()
            fg = "white" if (0.299*r + 0.587*g + 0.114*b) < 140 else "black"
            lbl.setStyleSheet(
                f"background:rgb({r},{g},{b}); color:{fg};"
                "border-radius:3px; font-size:11px; padding:1px 4px;"
            )
            return lbl

        legend.addWidget(QLabel("범례:"))
        legend.addWidget(_chip(_POS_STRONG,  "강한 매수(외인)"))
        legend.addWidget(_chip(_POS_MID,     "중간 매수"))
        legend.addWidget(_chip(_POS_WEAK,    "약한 매수"))
        legend.addWidget(_chip(_NEG_WEAK,    "약한 매도"))
        legend.addWidget(_chip(_NEG_MID,     "중간 매도"))
        legend.addWidget(_chip(_NEG_STRONG,  "강한 매도"))
        legend.addWidget(_chip(_CUR_COL_BG,  "현재 시각"))
        legend.addStretch()
        vl.addLayout(legend)

    # ── 데이터 로드 ──────────────────────────────────────────

    @staticmethod
    def _to_delta_df(df: pd.DataFrame) -> pd.DataFrame:
        """누적 일별 수급값 → 2분 구간 증감(Δ)으로 변환.

        첫 번째 슬롯: 값 그대로 (장 시작부터 해당 시각까지 누적).
        이후 슬롯: 직전 슬롯 값과의 차이 (2분간 신규 유입량).
        직전이 NaN인 경우 0으로 처리(첫 등장 슬롯으로 간주).
        """
        fixed = {"code", "name", "sector", "investor_type"}
        time_cols = sorted([c for c in df.columns if c not in fixed and ":" in str(c)])
        if len(time_cols) < 2:
            return df
        result = df.copy()
        for i in range(1, len(time_cols)):
            curr = time_cols[i]
            prev = time_cols[i - 1]
            curr_vals = df[curr]
            prev_vals = df[prev].fillna(0)   # NaN 이전 슬롯 = 0으로 처리
            delta = curr_vals - prev_vals
            result[curr] = delta.where(curr_vals.notna(), other=float("nan"))
        return result

    def load_today(self, date: Optional[str] = None) -> None:
        """SQLite에서 오늘(또는 지정 날짜) 스냅샷 로드 → 테이블 갱신."""
        raw = self._db.load_today(date)
        if not raw.empty:
            pivoted      = StockFlowDB.pivot_for_widget(raw)
            self._df     = self._to_delta_df(pivoted)   # 누적 → 2분 증감
            self._date   = raw["date"].iloc[0] if date is None else (date or "")
        else:
            self._df   = pd.DataFrame()
            self._date = date or datetime.datetime.now().strftime("%Y%m%d")

        self._refresh_date_combo()
        self._refresh_sector_combo()
        self._render_table()

    def set_live_prices(self, prices: dict) -> None:
        """현재가/전일대비/등락률/시가총액 실시간 갱신 — 가격 셀만 업데이트."""
        self._live_prices = prices
        self._refresh_price_cells()

    def _refresh_price_cells(self) -> None:
        """가격 컬럼(col 3~6)만 재렌더링 — 고정 테이블 대상 (전체 rebuild 없음)."""
        if not self._row_info or self._fixed_table.rowCount() == 0:
            return
        _RED  = QColor(0xC6, 0x28, 0x28)
        _BLUE = QColor(0x0D, 0x47, 0xA1)

        self._fixed_table.setUpdatesEnabled(False)
        for r_idx, (code, inv_type) in enumerate(self._row_info):
            if inv_type != "프로그램":
                continue
            info  = self._live_prices.get(code, {})
            price = int(info.get("현재가",   0) or 0)
            chg   = float(info.get("전일대비", 0) or 0)
            pct   = float(info.get("등락률",   0) or 0)
            cap   = int(info.get("시가총액",  0) or 0)

            bg = QBrush(self._fixed_table.item(r_idx, 0).background()
                        if self._fixed_table.item(r_idx, 0) else QColor(Qt.white))

            def _upd(col, text, fg=None, bold=False):
                it = self._fixed_table.item(r_idx, col) or QTableWidgetItem()
                it.setText(text)
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                it.setBackground(bg)
                if fg:   it.setForeground(QBrush(fg))
                if bold: f = it.font(); f.setBold(True); it.setFont(f)
                self._fixed_table.setItem(r_idx, col, it)

            _upd(3, f"{price:,}" if price else "—")
            _upd(4, f"{chg:+,.0f}" if chg else "—",
                 fg=(_RED if chg > 0 else _BLUE if chg < 0 else None))
            _upd(5, f"{pct:+.2f}%" if pct else "—",
                 fg=(_RED if pct > 0 else _BLUE if pct < 0 else None), bold=True)
            _upd(6, f"{cap:,}" if cap else "—")

        self._fixed_table.setUpdatesEnabled(True)

    def _auto_refresh(self) -> None:
        """60초마다 DB에서 최신 스냅샷 로드 — 배치 완료 콜백이 끊겨도 갱신 보장."""
        self.load_today(self._date or None)

    # ── 수급Δ 연계 컨트롤 ────────────────────────────────────

    def _on_delta_mode_changed(self, idx: int) -> None:
        """Δ 트리거 모드 변경 — 모드별 전용 위젯 표시."""
        self._delta_mode = idx
        show_surge = (idx == 1)
        show_instw = (idx == 2)
        self._delta_surge_lbl.setVisible(show_surge)
        self._delta_surge_spn.setVisible(show_surge)
        self._delta_instw_lbl.setVisible(show_instw)
        self._delta_instw_spn.setVisible(show_instw)

    def _set_delta_exec(self, exec_mode) -> None:
        """
        exec_mode=None → OFF
        exec_mode=0    → 반자동 (자동매매현황 추가, MA 로직에 위임)
        exec_mode=1    → 자동 (즉시 시장가 매수)
        """
        _on  = "background:#1a6a1a; color:#aaffaa; border:1px solid #4a9a4a;"
        _off = "background:#555; color:#ccc; border:1px solid #888;"
        _base = "border-radius:3px; font-size:11px; padding:2px 6px;"

        if exec_mode is None:
            self._delta_enabled   = False
            self._delta_mode_exec = 0
            self._delta_btn_off.setStyleSheet(_base + _on)
            self._delta_btn_semi.setStyleSheet(_base + _off)
            self._delta_btn_auto.setStyleSheet(_base + _off)
            self._delta_status_lbl.setText("● OFF")
            self._delta_status_lbl.setStyleSheet("color:#888; font-size:11px;")
        elif exec_mode == 0:
            self._delta_enabled   = True
            self._delta_mode_exec = 0
            self._delta_btn_off.setStyleSheet(_base + _off)
            self._delta_btn_semi.setStyleSheet(_base + _on)
            self._delta_btn_auto.setStyleSheet(_base + _off)
            self._delta_status_lbl.setText("🔔 반자동 대기중")
            self._delta_status_lbl.setStyleSheet("color:#88cc88; font-size:11px;")
        else:
            self._delta_enabled   = True
            self._delta_mode_exec = 1
            self._delta_btn_off.setStyleSheet(_base + _off)
            self._delta_btn_semi.setStyleSheet(_base + _off)
            self._delta_btn_auto.setStyleSheet(_base + "background:#6a1a1a; "
                                               "color:#ffaaaa; border:1px solid #aa4444;")
            self._delta_status_lbl.setText("⚡ 자동 활성")
            self._delta_status_lbl.setStyleSheet("color:#ff9999; font-size:11px;")

    def _on_time_vscroll(self, value: int) -> None:
        if not self._syncing_scroll:
            self._syncing_scroll = True
            self._fixed_table.verticalScrollBar().setValue(value)
            self._syncing_scroll = False

    def _on_fixed_vscroll(self, value: int) -> None:
        if not self._syncing_scroll:
            self._syncing_scroll = True
            self._table.verticalScrollBar().setValue(value)
            self._syncing_scroll = False

    @staticmethod
    def _generate_expected_slots() -> list[str]:
        """08:00 ~ min(현재시각, 20:00) 사이의 2분 단위 슬롯 목록."""
        now     = datetime.datetime.now()
        end_str = min(f"{now.hour:02d}:{now.minute:02d}", "20:00")
        slots: list[str] = []
        h, m = 8, 0
        while True:
            slot = f"{h:02d}:{m:02d}"
            if slot > end_str:
                break
            slots.append(slot)
            m += 2
            if m >= 60:
                m -= 60
                h += 1
            if h > 20:
                break
        return slots

    def _on_reset_today(self) -> None:
        """오늘 날짜 DB 데이터 전체 삭제 후 테이블 초기화."""
        import datetime as _dt
        date = self._date or _dt.datetime.now().strftime("%Y%m%d")
        y, m, d = date[:4], date[4:6], date[6:]
        ans = QMessageBox.question(
            self, "오늘 수급흐름 초기화",
            f"{y}-{m}-{d} 수급흐름 데이터를 모두 삭제하시겠습니까?\n"
            "(레거시 30분 간격 데이터도 함께 제거됩니다)",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        deleted = self._db.delete_date(date)
        self._df = __import__("pandas").DataFrame()
        self._time_slots = []
        self._row_info   = []
        self.load_today(date)
        self._last_lbl.setText(f"초기화 완료 ({deleted}행 삭제)")

    def append_snapshot(self, stock_rows: list[dict], snap_time: str = "") -> None:
        """실시간 스냅샷을 DB에 저장 후 테이블 갱신."""
        kwargs = {"snap_time": snap_time} if snap_time else {}
        saved  = self._db.save_snapshot(stock_rows, **kwargs)
        if saved > 0:
            now_str = datetime.datetime.now().strftime("%H:%M")
            self._last_lbl.setText(f"마지막 스냅샷: {now_str} ({saved}종목)")
            self.load_today(self._date or None)

    # ── 콤보 갱신 ────────────────────────────────────────────

    def _refresh_date_combo(self) -> None:
        dates = self._db.available_dates()
        today = datetime.datetime.now().strftime("%Y%m%d")
        if today not in dates:
            dates.insert(0, today)

        self._date_combo.blockSignals(True)
        self._date_combo.clear()
        for d in dates:
            y, m, day = d[:4], d[4:6], d[6:]
            self._date_combo.addItem(f"{y}-{m}-{day}", d)
        idx = next((i for i in range(self._date_combo.count())
                    if self._date_combo.itemData(i) == (self._date or today)), 0)
        self._date_combo.setCurrentIndex(idx)
        self._date_combo.blockSignals(False)

    def _refresh_sector_combo(self) -> None:
        sectors = sorted(self._df["sector"].dropna().unique()) if not self._df.empty else []
        cur = self._sector_combo.currentText()
        self._sector_combo.blockSignals(True)
        self._sector_combo.clear()
        self._sector_combo.addItem("전체")
        for s in sectors:
            self._sector_combo.addItem(s)
        idx = self._sector_combo.findText(cur)
        self._sector_combo.setCurrentIndex(max(idx, 0))
        self._sector_combo.blockSignals(False)

    # ── 이벤트 ───────────────────────────────────────────────

    def _on_date_changed(self, text: str) -> None:
        idx  = self._date_combo.currentIndex()
        date = self._date_combo.itemData(idx)
        if date:
            self._date = date
            self.load_today(date)

    def _on_sector_changed(self, text: str) -> None:
        self._sector_filter = "" if text == "전체" else text
        self._render_table()

    # ── 테이블 렌더 ──────────────────────────────────────────

    def _get_time_cols(self, df: pd.DataFrame) -> list[str]:
        """DataFrame에서 시간 컬럼(HH:MM 형식)만 추출해 정렬 반환."""
        fixed_set = {"code", "name", "sector", "investor_type"}
        return sorted([c for c in df.columns if c not in fixed_set and ":" in str(c)])

    def _render_table(self) -> None:
        df = self._df
        if df.empty:
            self._fixed_table.setRowCount(0)
            self._table.setRowCount(0)
            self._table.setColumnCount(0)
            self._time_slots = []
            return

        # 섹터 필터
        if self._sector_filter:
            df = df[df["sector"] == self._sector_filter]

        if df.empty:
            self._fixed_table.setRowCount(0)
            self._table.setRowCount(0)
            return

        # 실제 DB 시간 컬럼 + 2분 간격 기본 슬롯 병합
        db_slots   = self._get_time_cols(df)
        expected   = self._generate_expected_slots()
        time_slots = sorted(set(expected) | set(db_slots))

        # 시간 컬럼 변경 시 시간 테이블 구조 재설정
        if time_slots != self._time_slots:
            self._time_slots = time_slots
            self._table.setColumnCount(len(time_slots))
            self._table.setHorizontalHeaderLabels(time_slots)

            hdr = self._table.horizontalHeader()
            for i in range(len(time_slots)):
                hdr.setSectionResizeMode(i, QHeaderView.Fixed)
                self._table.setColumnWidth(i, 58)

            # 굵은 헤더 폰트 (고정 + 시간 테이블)
            bold = QFont(); bold.setBold(True); bold.setPixelSize(11)
            for i in range(_N_FIXED):
                item = self._fixed_table.horizontalHeaderItem(i)
                if item:
                    item.setFont(bold)
            for i in range(len(time_slots)):
                item = self._table.horizontalHeaderItem(i)
                if item:
                    item.setFont(bold)

        # 강한매수 기준 종목 정렬 — 최신 슬롯의 프로그램 순매수를 1차 키로
        latest_slot = None
        for t in reversed(time_slots):
            if t in df.columns and df[t].dropna().abs().gt(0).any():
                latest_slot = t
                break

        if latest_slot:
            # 프로그램 순매수를 1차 정렬 키, 기관+금투 합계를 2차 보조 키
            prog_df = df[df["investor_type"] == "프로그램"]
            prog_score = prog_df.set_index("code")[latest_slot].fillna(0).to_dict()

            inst_df  = df[df["investor_type"] != "프로그램"]
            inst_score = (
                inst_df.groupby("code")[latest_slot]
                .apply(lambda s: s.fillna(0).sum())
                .to_dict()
            ) if not inst_df.empty else {}

            _inv_order = {"프로그램": 0, "기관": 1, "금투": 2}
            df = df.copy()
            df["_prog"]      = df["code"].map(prog_score).fillna(0)
            df["_inst"]      = df["code"].map(inst_score).fillna(0)
            df["_inv_order"] = df["investor_type"].map(_inv_order).fillna(9)
            df = (df.sort_values(["_prog", "_inst", "_inv_order"], ascending=[False, False, True])
                    .drop(columns=["_prog", "_inst", "_inv_order"])
                    .reset_index(drop=True))

        # 시간 컬럼별 절대값 최대치 (색상 비율 계산용)
        time_abs_max: dict[str, float] = {}
        for t in time_slots:
            if t in df.columns:
                vals = df[t].dropna().abs()
                time_abs_max[t] = float(vals.max()) if not vals.empty else 1.0
            else:
                time_abs_max[t] = 1.0

        # 현재 시각 컬럼 인덱스 (self._table 기준)
        cur_slot = _current_slot()
        cur_col_idx = -1
        if time_slots:
            candidates = [t for t in time_slots if t <= cur_slot]
            if candidates:
                cur_col_idx = time_slots.index(candidates[-1])

        self._fixed_table.setRowCount(len(df))
        self._table.setRowCount(len(df))
        self._row_info = []
        self._fixed_table.setUpdatesEnabled(False)
        self._table.setUpdatesEnabled(False)
        try:
            self._render_rows(df, time_slots, time_abs_max, cur_col_idx)
        except Exception as _e:
            import traceback
            traceback.print_exc()
        finally:
            self._fixed_table.setUpdatesEnabled(True)
            self._table.setUpdatesEnabled(True)

        self._highlight_current_col()
        self._fixed_table.resizeRowsToContents()
        for r in range(self._fixed_table.rowCount()):
            self._table.setRowHeight(r, self._fixed_table.rowHeight(r))

        # 고정 테이블 너비 자동 계산 (데이터 반영 후)
        fixed_w = sum(self._fixed_table.columnWidth(c) for c in range(_N_FIXED)) + 2
        self._fixed_table.setFixedWidth(fixed_w)

        # 최신 컬럼으로 자동 스크롤
        if cur_col_idx >= 0:
            self._table.scrollTo(
                self._table.model().index(0, cur_col_idx),
                QAbstractItemView.PositionAtCenter,
            )

    def _render_rows(self, df, time_slots, time_abs_max, cur_col_idx) -> None:
        """행 렌더링 로직 분리 — setUpdatesEnabled try-finally 내부에서 호출."""

        codes = df["code"].tolist()
        code_groups: dict[str, int] = {}
        g_idx = 0
        for code in codes:
            if code not in code_groups:
                code_groups[code] = g_idx
                g_idx += 1

        _RED   = QColor(0xC6, 0x28, 0x28)
        _BLUE  = QColor(0x0D, 0x47, 0xA1)

        for r_idx, (_, row) in enumerate(df.iterrows()):
            code     = row["code"]
            name     = row["name"]
            sector   = row["sector"]
            inv_type = row["investor_type"]
            grp_bg   = _GROUP_BG[code_groups.get(code, 0) % 2]
            self._row_info.append((code, inv_type))

            # ── 고정 테이블 col 0: 종목명 ──────────────────────
            name_text = name if inv_type == "프로그램" else ""
            it0 = QTableWidgetItem(name_text)
            it0.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            it0.setBackground(QBrush(grp_bg))
            if inv_type == "프로그램":
                f = it0.font(); f.setBold(True); it0.setFont(f)
            self._fixed_table.setItem(r_idx, 0, it0)

            # col 1: 섹터
            sec_text = sector if inv_type == "프로그램" else ""
            it1 = QTableWidgetItem(sec_text)
            it1.setTextAlignment(Qt.AlignCenter)
            it1.setBackground(QBrush(grp_bg))
            self._fixed_table.setItem(r_idx, 1, it1)

            # col 2: 구분
            it2 = QTableWidgetItem(inv_type)
            it2.setTextAlignment(Qt.AlignCenter)
            it2.setBackground(QBrush(grp_bg))
            it2.setForeground(QBrush(_INV_LABEL_COLOR.get(inv_type, _TEXT_DARK)))
            f2 = it2.font(); f2.setBold(True); it2.setFont(f2)
            self._fixed_table.setItem(r_idx, 2, it2)

            # col 3~6: 가격 정보 (프로그램 행에만 표시)
            if inv_type == "프로그램":
                info = self._live_prices.get(code, {})
                price = int(info.get("현재가",   0) or 0)
                chg   = float(info.get("전일대비", 0) or 0)
                pct   = float(info.get("등락률",   0) or 0)
                cap   = int(info.get("시가총액",  0) or 0)

                it3 = QTableWidgetItem(f"{price:,}" if price else "—")
                it3.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                it3.setBackground(QBrush(grp_bg))
                self._fixed_table.setItem(r_idx, 3, it3)

                it4 = QTableWidgetItem(f"{chg:+,.0f}" if chg else "—")
                it4.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                it4.setBackground(QBrush(grp_bg))
                if chg > 0:   it4.setForeground(QBrush(_RED))
                elif chg < 0: it4.setForeground(QBrush(_BLUE))
                self._fixed_table.setItem(r_idx, 4, it4)

                it5 = QTableWidgetItem(f"{pct:+.2f}%" if pct else "—")
                it5.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                it5.setBackground(QBrush(grp_bg))
                if pct > 0:   it5.setForeground(QBrush(_RED))
                elif pct < 0: it5.setForeground(QBrush(_BLUE))
                f5 = it5.font(); f5.setBold(True); it5.setFont(f5)
                self._fixed_table.setItem(r_idx, 5, it5)

                it6 = QTableWidgetItem(f"{cap:,}" if cap else "—")
                it6.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                it6.setBackground(QBrush(grp_bg))
                self._fixed_table.setItem(r_idx, 6, it6)
            else:
                for c in range(3, 7):
                    it = QTableWidgetItem("")
                    it.setBackground(QBrush(grp_bg))
                    self._fixed_table.setItem(r_idx, c, it)

            # ── 시간 테이블 (c_idx = self._table의 컬럼 인덱스) ─
            # Δ값 히스토리: 급증 감지용 (최근 3개 슬롯 평균과 비교)
            delta_history: list[float] = []
            for c_idx, t in enumerate(time_slots):
                val = row.get(t, float("nan"))
                is_nan = math.isnan(val) if isinstance(val, float) else False

                if is_nan:
                    cell_text = "—"
                    bg = _CUR_COL_BG if c_idx == cur_col_idx else grp_bg
                    fg = _TEXT_DARK
                    bold = False
                elif float(val) == 0:
                    # Δ=0: 변화 없음 — 데이터 없음("—")과 구분하여 중립 표시
                    cell_text = "·"
                    bg = _CUR_COL_BG if c_idx == cur_col_idx else grp_bg
                    fg = QColor(0xAA, 0xAA, 0xAA)
                    bold = False
                    delta_history.append(0.0)
                else:
                    val = float(val)
                    cell_text = f"{val:+,.0f}"
                    bg = _net_bg(val, time_abs_max.get(t, 1.0))
                    if c_idx == cur_col_idx:
                        bg = _CUR_COL_BG
                    fg = _text_color(bg)
                    # 급증 감지: 직전 3구간 평균 절대값의 2배 초과 → 굵게
                    avg3 = (sum(abs(v) for v in delta_history[-3:]) / len(delta_history[-3:])
                            if delta_history else 0)
                    bold = (abs(val) >= max(avg3 * 2, time_abs_max.get(t, 1.0) * 0.4))
                    delta_history.append(val)

                it = QTableWidgetItem(cell_text)
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                it.setBackground(QBrush(bg))
                it.setForeground(QBrush(fg))
                if bold:
                    f = it.font(); f.setBold(True); it.setFont(f)

                self._table.setItem(r_idx, c_idx, it)

    def _highlight_current_col(self) -> None:
        """현재 시각에 가장 가까운 컬럼 헤더를 노란 배경으로 강조."""
        if not self._time_slots:
            return

        cur_slot = _current_slot()
        candidates = [t for t in self._time_slots if t <= cur_slot]
        matched = candidates[-1] if candidates else None

        yellow  = QColor(0xFF, 0xFF, 0x00)
        default = QColor(Qt.transparent)

        for i, t in enumerate(self._time_slots):
            item = self._table.horizontalHeaderItem(i)
            if item is None:
                continue
            if t == matched:
                item.setBackground(QBrush(yellow))
                f = item.font(); f.setBold(True); item.setFont(f)
            else:
                item.setBackground(QBrush(default))
