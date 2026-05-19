# -*- coding: utf-8 -*-
"""
market_map.py — 업종별 시장지도 + 종가베팅 섹터 스코어링

트리맵 시각화 (순수 Python + QPainter):
  - 블록 크기 ∝ 거래대금합
  - 색상: 빨강(상승) / 파랑(하락) — 한국 주식 색상 관습
  - 30초 자동 갱신

섹터 스코어링 (종가베팅):
  - 외인수급 40점 | 거래대금 30점 | 등락률 20점 | 확산도 10점
  - TOP3 종베 추천 배지 + [종베 스캔] 버튼 → scan_popup 연동

사용법:
    widget = MarketMapWidget(trader=self)
    tab_widget.addTab(widget, "시장지도")
"""
from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from PyQt5.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QPushButton, QFrame, QTableWidget, QTableWidgetItem,
    QHeaderView, QSplitter, QScrollArea, QAbstractItemView,
    QSizePolicy,
)
from PyQt5.QtCore import Qt, QTimer, QRect, pyqtSignal
from PyQt5.QtGui import QPainter, QColor, QPen, QFont, QBrush, QFontMetrics

try:
    from loguru import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# 색상 헬퍼
# ─────────────────────────────────────────────────────────────────

def _pct_color(pct: float) -> QColor:
    """등락률 → 배경색 (한국 관습: 빨강=상승, 파랑=하락)."""
    if pct >= 5.0:
        return QColor(0xC6, 0x28, 0x28)
    if pct >= 2.0:
        return QColor(0xE5, 0x37, 0x35)
    if pct >= 0.5:
        return QColor(0xEF, 0x9A, 0x9A)
    if pct >= -0.5:
        return QColor(0xF0, 0xF0, 0xF0)
    if pct >= -2.0:
        return QColor(0x90, 0xCA, 0xF9)
    if pct >= -5.0:
        return QColor(0x19, 0x76, 0xD2)
    return QColor(0x0D, 0x47, 0xA1)


def _text_color(bg: QColor) -> QColor:
    lum = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
    return QColor(Qt.white) if lum < 150 else QColor(Qt.black)


# ─────────────────────────────────────────────────────────────────
# Squarify 트리맵 레이아웃 (pure Python)
# ─────────────────────────────────────────────────────────────────

def _worst_ratio(row: list[float], side: float) -> float:
    s = sum(row)
    if s <= 0 or side <= 0:
        return float("inf")
    return max(max(s * s / (side * side * r), r * side * side / (s * s)) for r in row)


def _squarify(sizes: list[float], x: float, y: float, w: float, h: float) -> list[dict]:
    """
    sizes: 양수 값 리스트 (면적 비중, 정규화 전)
    Returns: [{"x", "y", "w", "h", "idx"}, ...]  — idx는 입력 sizes 인덱스
    """
    if not sizes or w <= 0 or h <= 0:
        return []
    total = sum(sizes)
    if total <= 0:
        return []
    area = w * h
    norm = [s / total * area for s in sizes]
    rects: list[dict] = []
    _sq_impl(norm, 0, x, y, w, h, rects)
    return rects


def _sq_impl(norm: list[float], start: int, x: float, y: float,
             w: float, h: float, rects: list[dict]) -> None:
    n = len(norm)
    if start >= n:
        return
    if start == n - 1:
        rects.append({"x": x, "y": y, "w": w, "h": h, "idx": start})
        return

    side = min(w, h)
    row: list[float] = [norm[start]]
    i = start + 1
    while i < n:
        trial = row + [norm[i]]
        if _worst_ratio(trial, side) <= _worst_ratio(row, side):
            row.append(norm[i])
            i += 1
        else:
            break

    end = start + len(row)
    total = sum(norm[start:])
    row_sum = sum(row)

    if total <= 0:
        return

    if w >= h:
        row_w = row_sum / total * w
        cy = y
        for j, s in enumerate(row):
            cell_h = s / row_sum * h if row_sum > 0 else h / len(row)
            rects.append({"x": x, "y": cy, "w": row_w, "h": cell_h, "idx": start + j})
            cy += cell_h
        _sq_impl(norm, end, x + row_w, y, w - row_w, h, rects)
    else:
        row_h = row_sum / total * h
        cx = x
        for j, s in enumerate(row):
            cell_w = s / row_sum * w if row_sum > 0 else w / len(row)
            rects.append({"x": cx, "y": y, "w": cell_w, "h": row_h, "idx": start + j})
            cx += cell_w
        _sq_impl(norm, end, x, y + row_h, w, h - row_h, rects)


# ─────────────────────────────────────────────────────────────────
# SectorScorer — 종가베팅용 섹터 점수
# ─────────────────────────────────────────────────────────────────

class SectorScorer:
    """
    섹터별 종가베팅 매력도 점수 (0~100).

    가중치:
        외인수급    20점  (외인순매수합 퍼센타일)
        기관수급    12점  (기관순매수합 퍼센타일)
        금융투자     8점  (금융투자순매수합 퍼센타일)
        거래대금    30점  (거래대금합 퍼센타일)
        등락률      20점  (평균등락률 퍼센타일)
        확산도      10점  (확산도 퍼센타일)
    """

    WEIGHTS = {"foreign": 20, "inst": 12, "fin": 8, "volume": 30, "change": 20, "spread": 10}

    def score(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        summary_df(get_summary() 반환값)를 받아 'sector_score', 'sector_rank' 컬럼을 추가해 반환.
        점수 내림차순 정렬.
        """
        if df is None or df.empty:
            return df

        df = df.copy()
        n = len(df)
        if n == 1:
            df["sector_score"] = 50.0
            df["sector_rank"]  = 1
            return df

        def _prank(col: str) -> "pd.Series":
            vals = df[col].fillna(0)
            return vals.rank(method="average", pct=True) * 100

        foreign_pct = _prank("외인순매수합(주)")
        inst_pct    = _prank("기관순매수합(주)")
        fin_pct     = _prank("금융투자순매수합(주)")
        volume_pct  = _prank("거래대금합(억)")
        change_pct  = _prank("평균등락률(%)")
        spread_pct  = _prank("확산도(%)")

        df["sector_score"] = (
            foreign_pct * self.WEIGHTS["foreign"] / 100 +
            inst_pct    * self.WEIGHTS["inst"]    / 100 +
            fin_pct     * self.WEIGHTS["fin"]     / 100 +
            volume_pct  * self.WEIGHTS["volume"]  / 100 +
            change_pct  * self.WEIGHTS["change"]  / 100 +
            spread_pct  * self.WEIGHTS["spread"]  / 100
        ).round(1)

        df = df.sort_values("sector_score", ascending=False).reset_index(drop=True)
        df["sector_rank"] = range(1, len(df) + 1)
        return df


_scorer = SectorScorer()


# ─────────────────────────────────────────────────────────────────
# HeatmapView — QPainter 트리맵 위젯
# ─────────────────────────────────────────────────────────────────

class HeatmapView(QWidget):
    """업종별 트리맵. 클릭 시 sector_clicked 시그널 발생."""

    sector_clicked = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rects: list[dict] = []   # [{x,y,w,h,idx,sector,pct,vol}, ...]
        self._selected: str = ""
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setToolTip("업종을 클릭하면 구성 종목을 확인할 수 있습니다")

    def set_data(self, df: pd.DataFrame) -> None:
        """scored summary_df 수신. 레이아웃 재계산 후 repaint."""
        if df is None or df.empty:
            self._rects = []
            self.update()
            return

        sizes  = df["거래대금합(억)"].fillna(0).tolist()
        sectors = df["섹터명"].tolist()
        pcts    = df["평균등락률(%)"].fillna(0).tolist()
        vols    = df["거래대금합(억)"].fillna(0).tolist()

        pw = max(self.width(),  1)
        ph = max(self.height(), 1)
        layout = _squarify(sizes, 0, 0, pw, ph)

        self._rects = []
        for r in layout:
            idx = r["idx"]
            if idx >= len(sectors):
                continue
            self._rects.append({
                "x": r["x"], "y": r["y"], "w": r["w"], "h": r["h"],
                "sector": sectors[idx],
                "pct":    pcts[idx],
                "vol":    vols[idx],
            })

        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # resizeEvent 시 레이아웃 재계산은 set_data 재호출 필요 → 부모가 담당
        # (MarketMapWidget._resize_timer로 처리)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)

        bg = QColor(0x21, 0x21, 0x21)
        painter.fillRect(self.rect(), bg)

        if not self._rects:
            painter.setPen(QColor(Qt.white))
            painter.drawText(self.rect(), Qt.AlignCenter, "데이터 없음\n(거래대금상위 조회 후 갱신됩니다)")
            return

        font_normal = QFont()
        font_normal.setPixelSize(12)
        font_bold   = QFont()
        font_bold.setPixelSize(12)
        font_bold.setBold(True)

        for r in self._rects:
            rx, ry = int(r["x"]) + 1, int(r["y"]) + 1
            rw, rh = max(int(r["w"]) - 2, 1), max(int(r["h"]) - 2, 1)
            rect   = QRect(rx, ry, rw, rh)

            bg_col = _pct_color(r["pct"])
            is_sel = (r["sector"] == self._selected)
            if is_sel:
                # 선택된 섹터: 테두리 강조
                painter.fillRect(rect, bg_col)
                painter.setPen(QPen(QColor(0xFF, 0xD7, 0x00), 3))
                painter.drawRect(rect)
            else:
                painter.fillRect(rect, bg_col)
                painter.setPen(QPen(QColor(0x33, 0x33, 0x33), 1))
                painter.drawRect(rect)

            if rw < 20 or rh < 14:
                continue

            tc = _text_color(bg_col)
            painter.setPen(tc)

            sector = r["sector"]
            pct    = r["pct"]
            pct_str = f"{pct:+.1f}%"

            if rh >= 36 and rw >= 50:
                painter.setFont(font_bold)
                # 이름
                painter.drawText(QRect(rx+3, ry+3, rw-6, 16),
                                 Qt.AlignLeft | Qt.AlignTop, sector)
                painter.setFont(font_normal)
                painter.drawText(QRect(rx+3, ry+20, rw-6, 14),
                                 Qt.AlignLeft | Qt.AlignTop, pct_str)
            else:
                painter.setFont(font_normal)
                combined = f"{sector} {pct_str}"
                painter.drawText(rect.adjusted(3, 2, -3, -2),
                                 Qt.AlignLeft | Qt.AlignVCenter, combined)

        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        mx, my = event.x(), event.y()
        for r in self._rects:
            if (r["x"] <= mx < r["x"] + r["w"] and
                    r["y"] <= my < r["y"] + r["h"]):
                self._selected = r["sector"]
                self.update()
                self.sector_clicked.emit(r["sector"])
                return


# ─────────────────────────────────────────────────────────────────
# SectorRankTable — 섹터 순위표
# ─────────────────────────────────────────────────────────────────

class SectorRankTable(QWidget):
    """종가베팅 점수 기준 섹터 순위 테이블."""

    sector_clicked = pyqtSignal(str)

    _COLS = ["순위", "섹터명", "종베점수", "거래대금", "등락률", "프로그램(백만)", "기관수급(백만)", "금융투자(백만)", "확산도"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(4)

        lbl = QLabel("섹터 종가베팅 점수")
        lbl.setStyleSheet("font-weight:bold; font-size:12px; padding:4px;")
        vl.addWidget(lbl)

        self._table = QTableWidget(0, len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for c in [0, 2, 3, 4, 5, 6, 7, 8]:
            self._table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self._table.verticalHeader().hide()
        self._table.setStyleSheet("font-size:11px;")
        self._table.itemClicked.connect(self._on_item_clicked)
        vl.addWidget(self._table)

    def _on_item_clicked(self, item):
        row = item.row()
        name_item = self._table.item(row, 1)
        if name_item:
            self.sector_clicked.emit(name_item.text())

    def refresh(self, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            self._table.setRowCount(0)
            return

        self._table.setRowCount(len(df))
        for r_idx, (_, row) in enumerate(df.iterrows()):
            rank  = int(row.get("sector_rank", r_idx + 1))
            name  = str(row.get("섹터명", ""))
            score = float(row.get("sector_score", 0))
            vol   = float(row.get("거래대금합(억)", 0))
            chg   = float(row.get("평균등락률(%)", 0))
            fore  = float(row.get("외인순매수합(주)", 0))
            inst  = float(row.get("기관순매수합(주)", 0))
            fin   = float(row.get("금융투자순매수합(주)", 0))
            spr   = float(row.get("확산도(%)", 0))

            chg_color = QColor(0xC6, 0x28, 0x28) if chg >= 0 else QColor(0x0D, 0x47, 0xA1)

            def _item(text: str, align=Qt.AlignCenter, fg=None) -> QTableWidgetItem:
                it = QTableWidgetItem(text)
                it.setTextAlignment(align)
                if fg:
                    it.setForeground(fg)
                return it

            score_color = (QColor(0xC6, 0x28, 0x28) if score >= 70
                           else QColor(0xE6, 0x8A, 0x00) if score >= 50
                           else QColor(0x55, 0x55, 0x55))

            vol_str  = f"{vol/10000:.1f}조" if vol >= 10000 else f"{vol:,.0f}억"
            fore_str = f"{fore:+,.0f}" if fore != 0 else "—"
            inst_str = f"{inst:+,.0f}" if inst != 0 else "—"
            fin_str  = f"{fin:+,.0f}"  if fin  != 0 else "—"

            def _net_color(val: float):
                if val > 0:  return QColor(0xC6, 0x28, 0x28)
                if val < 0:  return QColor(0x0D, 0x47, 0xA1)
                return None

            self._table.setItem(r_idx, 0, _item(str(rank)))
            self._table.setItem(r_idx, 1, _item(name, Qt.AlignLeft | Qt.AlignVCenter))
            self._table.setItem(r_idx, 2, _item(f"{score:.0f}점", fg=score_color))
            self._table.setItem(r_idx, 3, _item(vol_str))
            self._table.setItem(r_idx, 4, _item(f"{chg:+.2f}%", fg=chg_color))
            self._table.setItem(r_idx, 5, _item(fore_str, fg=_net_color(fore)))
            self._table.setItem(r_idx, 6, _item(inst_str, fg=_net_color(inst)))
            self._table.setItem(r_idx, 7, _item(fin_str,  fg=_net_color(fin)))
            self._table.setItem(r_idx, 8, _item(f"{spr:.0f}%"))
        self._table.resizeRowsToContents()


# ─────────────────────────────────────────────────────────────────
# TOP3 배지
# ─────────────────────────────────────────────────────────────────

class _Top3Badge(QFrame):
    def __init__(self, rank: int, parent=None) -> None:
        super().__init__(parent)
        self._rank = rank
        colors = {1: ("#C62828", "#FFEBEE"), 2: ("#E65100", "#FFF3E0"), 3: ("#1565C0", "#E3F2FD")}
        border, bg = colors.get(rank, ("#555", "#f5f5f5"))
        self.setStyleSheet(
            f"QFrame {{ background:{bg}; border:2px solid {border};"
            "border-radius:6px; padding:4px; }}"
        )
        self.setFixedHeight(52)

        hl = QHBoxLayout(self)
        hl.setContentsMargins(8, 4, 8, 4)

        rank_lbl = QLabel(f"{rank}위")
        rank_lbl.setStyleSheet(
            f"color:{border}; font-weight:bold; font-size:13px; border:none;"
        )
        hl.addWidget(rank_lbl)

        self._name_lbl  = QLabel("—")
        self._score_lbl = QLabel("")
        self._name_lbl.setStyleSheet("font-size:12px; font-weight:bold; border:none;")
        self._score_lbl.setStyleSheet(f"font-size:11px; color:{border}; border:none;")
        hl.addWidget(self._name_lbl)
        hl.addStretch()
        hl.addWidget(self._score_lbl)

    def set_data(self, name: str, score: float, chg: float) -> None:
        self._name_lbl.setText(name)
        chg_str = f"{chg:+.1f}%"
        self._score_lbl.setText(f"{score:.0f}점  {chg_str}")

    def clear(self) -> None:
        self._name_lbl.setText("—")
        self._score_lbl.setText("")


# ─────────────────────────────────────────────────────────────────
# StockListPanel — 선택 섹터 구성 종목
# ─────────────────────────────────────────────────────────────────

class StockListPanel(QWidget):
    _COLS = ["종목명", "현재가", "등락률", "거래대금(억)", "프로그램(백만)", "기관수급(백만)", "금융투자(백만)"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(2)

        self._title = QLabel("섹터 구성 종목")
        self._title.setStyleSheet(
            "font-weight:bold; font-size:12px; padding:4px 4px 2px 4px;"
            "border-bottom:1px solid #e0e0e0;"
        )
        vl.addWidget(self._title)

        self._table = QTableWidget(0, len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in [1, 2, 3, 4, 5, 6]:
            self._table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self._table.verticalHeader().hide()
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet("font-size:11px;")
        vl.addWidget(self._table)

    def refresh(self, sector: str, df: pd.DataFrame) -> None:
        self._title.setText(f"▶ {sector} 구성 종목 ({len(df)}개)")

        self._table.setRowCount(len(df))
        for r_idx, (_, row) in enumerate(df.iterrows()):
            name = str(row.get("종목명", ""))
            price = int(row.get("현재가", 0) or 0)
            chg   = float(row.get("등락률(%)", 0) or 0)
            vol   = float(row.get("거래대금(억)", 0) or 0)
            fore  = float(row.get("외인순매수(주)", 0) or 0)
            inst  = float(row.get("기관순매수(주)", 0) or 0)
            fin   = float(row.get("금융투자순매수(주)", 0) or 0)

            chg_color = QColor(0xC6, 0x28, 0x28) if chg >= 0 else QColor(0x0D, 0x47, 0xA1)

            def _it(text, align=Qt.AlignCenter, fg=None):
                it = QTableWidgetItem(text)
                it.setTextAlignment(align)
                if fg:
                    it.setForeground(fg)
                return it

            def _net_clr(v):
                if math.isnan(v): return None
                return QColor(0xC6, 0x28, 0x28) if v > 0 else (QColor(0x0D, 0x47, 0xA1) if v < 0 else None)

            vol_str  = f"{vol:,.0f}"
            fore_str = f"{fore:+,.0f}" if not math.isnan(fore) else "—"
            inst_str = f"{inst:+,.0f}" if not math.isnan(inst) else "—"
            fin_str  = f"{fin:+,.0f}"  if not math.isnan(fin)  else "—"

            self._table.setItem(r_idx, 0, _it(name, Qt.AlignLeft | Qt.AlignVCenter))
            self._table.setItem(r_idx, 1, _it(f"{price:,}"))
            self._table.setItem(r_idx, 2, _it(f"{chg:+.2f}%", fg=chg_color))
            self._table.setItem(r_idx, 3, _it(vol_str))
            self._table.setItem(r_idx, 4, _it(fore_str, fg=_net_clr(fore)))
            self._table.setItem(r_idx, 5, _it(inst_str, fg=_net_clr(inst)))
            self._table.setItem(r_idx, 6, _it(fin_str,  fg=_net_clr(fin)))
        self._table.resizeRowsToContents()


# ─────────────────────────────────────────────────────────────────
# MarketMapWidget — 메인 컨테이너
# ─────────────────────────────────────────────────────────────────

class MarketMapWidget(QWidget):
    """
    시장지도 + 섹터 종가베팅 점수 메인 위젯.

    레이아웃:
        ┌──────────────────────┬──────────────────┐
        │  HeatmapView (트리맵) │  SectorRankTable │
        │                      │  TOP3 배지        │
        │                      │  [종베 스캔] 버튼 │
        └──────────────────────┴──────────────────┘
        │  StockListPanel (선택 섹터 구성종목)       │
        └────────────────────────────────────────────┘
    """

    def __init__(self, trader=None, parent=None) -> None:
        super().__init__(parent)
        self._trader         = trader
        self._scored_df: Optional[pd.DataFrame] = None
        self._selected_sector: str = ""

        self._build_ui()

        # 30초 자동 갱신
        self._refresh_timer = QTimer()
        self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.start(30_000)

        # resize 디바운스 (트리맵 재계산)
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._redraw_heatmap)

        # 초기 갱신
        QTimer.singleShot(500, self.refresh)

    # ── UI 빌드 ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        main_vl = QVBoxLayout(self)
        main_vl.setContentsMargins(4, 4, 4, 4)
        main_vl.setSpacing(4)

        # ── 상단 툴바 ─────────────────────────────────────────
        tool_hl = QHBoxLayout()

        title = QLabel("🗺 업종별 시장지도")
        title.setStyleSheet("font-weight:bold; font-size:13px;")
        tool_hl.addWidget(title)
        tool_hl.addStretch()

        self._last_update_lbl = QLabel("마지막 갱신: —")
        self._last_update_lbl.setStyleSheet("color:#888; font-size:11px;")
        tool_hl.addWidget(self._last_update_lbl)

        refresh_btn = QPushButton("🔄 지금 갱신")
        refresh_btn.setFixedWidth(90)
        refresh_btn.clicked.connect(self.refresh)
        tool_hl.addWidget(refresh_btn)

        main_vl.addLayout(tool_hl)

        # ── TOP3 배지 행 ──────────────────────────────────────
        badge_hl = QHBoxLayout()
        badge_hl.setSpacing(6)
        self._badges = [_Top3Badge(i + 1) for i in range(3)]
        for b in self._badges:
            badge_hl.addWidget(b)

        self._scan_btn = QPushButton("⚡ 종베 스캔 (HOT 섹터 우선)")
        self._scan_btn.setStyleSheet(
            "QPushButton { background:#C62828; color:white; font-weight:bold;"
            "border-radius:4px; padding:0 12px; }"
            "QPushButton:hover { background:#E53935; }"
        )
        self._scan_btn.setFixedHeight(52)
        self._scan_btn.clicked.connect(self._on_scan_clicked)
        badge_hl.addWidget(self._scan_btn)
        main_vl.addLayout(badge_hl)

        # ── 중단: 트리맵 + 섹터 순위표 ────────────────────────
        mid_splitter = QSplitter(Qt.Horizontal)

        self._heatmap = HeatmapView()
        self._heatmap.sector_clicked.connect(self._on_sector_clicked)
        mid_splitter.addWidget(self._heatmap)

        self._rank_table = SectorRankTable()
        self._rank_table.sector_clicked.connect(self._on_sector_clicked)
        mid_splitter.addWidget(self._rank_table)

        mid_splitter.setStretchFactor(0, 6)
        mid_splitter.setStretchFactor(1, 4)
        main_vl.addWidget(mid_splitter, stretch=5)

        # ── 하단: 선택 섹터 구성 종목 ─────────────────────────
        self._stock_panel = StockListPanel()
        self._stock_panel.setMaximumHeight(180)
        main_vl.addWidget(self._stock_panel, stretch=2)

    # ── 데이터 갱신 ──────────────────────────────────────────────

    def refresh(self) -> None:
        """sector_analyzer.get_summary() → score → UI 갱신."""
        if self._trader is None:
            self._show_demo()
            return

        try:
            summary_df = self._trader.sector_analyzer.get_summary()
        except Exception as e:
            logger.debug(f"[시장지도] get_summary 실패: {e}")
            return

        if summary_df is None or summary_df.empty:
            return

        self._update_from_df(summary_df)

    def update_from_summary(self, summary_df: pd.DataFrame) -> None:
        """_on_sector_summary_updated 콜백에서 직접 호출 (실시간 갱신)."""
        if summary_df is None or summary_df.empty:
            return
        self._update_from_df(summary_df)

    def _inject_prog_data(self, summary_df: pd.DataFrame) -> pd.DataFrame:
        """외인순매수합(주) → 프로그램매매 섹터 합계로 교체."""
        if self._trader is None:
            return summary_df
        prog_cache = getattr(self._trader, '_prog_trade_cache', {})
        if not prog_cache:
            return summary_df
        try:
            sector_prog: dict[str, float] = {}
            for code, stock in self._trader.sector_analyzer._stocks.items():
                val = prog_cache.get(code)
                if val is not None:
                    sector_prog[stock.sector] = sector_prog.get(stock.sector, 0.0) + val
            df = summary_df.copy()
            df["외인순매수합(주)"] = df["섹터명"].map(
                lambda s: sector_prog.get(s, float("nan")))
            return df
        except Exception:
            return summary_df

    def _update_from_df(self, summary_df: pd.DataFrame) -> None:
        summary_df = self._inject_prog_data(summary_df)
        try:
            scored = _scorer.score(summary_df)
        except Exception as e:
            logger.debug(f"[시장지도] 점수 계산 실패: {e}")
            return

        self._scored_df = scored
        self._redraw_heatmap()
        self._rank_table.refresh(scored)
        self._update_badges(scored)

        import datetime
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        self._last_update_lbl.setText(f"마지막 갱신: {now_str}")

        # 선택된 섹터가 있으면 재갱신
        if self._selected_sector:
            self._on_sector_clicked(self._selected_sector)

    def _redraw_heatmap(self) -> None:
        if self._scored_df is not None:
            self._heatmap.set_data(self._scored_df)

    def _update_badges(self, df: pd.DataFrame) -> None:
        top3 = df.head(3)
        for i, badge in enumerate(self._badges):
            if i < len(top3):
                row = top3.iloc[i]
                badge.set_data(
                    str(row["섹터명"]),
                    float(row["sector_score"]),
                    float(row["평균등락률(%)"]),
                )
            else:
                badge.clear()

    def _on_sector_clicked(self, sector: str) -> None:
        self._selected_sector = sector
        self._heatmap._selected = sector
        self._heatmap.update()

        if self._trader is None:
            return
        try:
            stock_df = self._trader.sector_analyzer.get_sector_stocks(sector)
            # 외인순매수(주) → 프로그램매매 데이터로 교체
            prog_cache = getattr(self._trader, '_prog_trade_cache', {})
            if prog_cache and "종목코드" in stock_df.columns and not stock_df.empty:
                stock_df = stock_df.copy()
                stock_df["외인순매수(주)"] = stock_df["종목코드"].map(
                    lambda c: prog_cache.get(c, float("nan")))
            self._stock_panel.refresh(sector, stock_df)
        except Exception as e:
            logger.debug(f"[시장지도] 섹터 구성종목 조회 실패: {e}")

    def _on_scan_clicked(self) -> None:
        hot_sectors = self.get_hot_sectors(5)
        try:
            from scan_popup import ScanEngine, ScanPopup
            from PyQt5.QtCore import QSettings as _QS
            s = _QS("StockCoding", "ScanPopup")
            candidates = ScanEngine(self._trader).run_scan(
                score_min    = s.value("score_min",    70,  int),
                pullback_min = s.value("pullback_min", 3.0, float),
                pullback_max = s.value("pullback_max", 7.0, float),
                target_pct   = s.value("target_pct",   3.5, float),
                stop_pct     = s.value("stop_pct",     3.0, float),
                hot_sectors  = hot_sectors,
            )
            win = ScanPopup(candidates, trader=self._trader, parent=self)
            win.setWindowTitle(
                f"종가베팅 스캔 — HOT 섹터 우선 ({', '.join(hot_sectors[:3])})"
                if hot_sectors else "종가베팅 스캔"
            )
            win.show()
            self._scan_win = win   # GC 방지
        except Exception as e:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(self, "종베 스캔", f"스캔 창 열기 실패: {e}")

    def _show_demo(self) -> None:
        """trader 없을 때 데모 데이터 표시."""
        demo_data = {
            "섹터명":          ["반도체", "2차전지", "바이오", "기계/방산", "금융", "자동차"],
            "거래대금합(억)":   [8500, 6200, 3100, 2800, 2100, 1900],
            "평균등락률(%)":    [2.3, -1.1, 0.5, 4.2, -0.3, 1.7],
            "확산도(%)":        [72, 38, 55, 85, 45, 68],
            "외인순매수합(주)": [320, -180, 90, 450, -120, 200],
            "기관순매수합(주)": [150, -90, 40, 280, 60, 110],
            "대장주":          ["SK하이닉스", "LG에너지솔루션", "삼성바이오", "한화에어로", "KB금융", "현대차"],
            "종목수":          [12, 8, 15, 6, 9, 7],
        }
        self._update_from_df(pd.DataFrame(demo_data))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_timer.start(150)

    def get_hot_sectors(self, n: int = 5) -> list[str]:
        """상위 n개 HOT 섹터 이름 반환 (scan_popup 연동용)."""
        if self._scored_df is None or self._scored_df.empty:
            return []
        return self._scored_df["섹터명"].head(n).tolist()
