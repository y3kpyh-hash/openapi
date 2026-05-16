# -*- coding: utf-8 -*-
"""
스윙 보유종목 대시보드 (SwingDashboard)

4개 섹션:
  섹션1: 보유종목 메인 테이블 (12열, 행 색상, 인라인 버튼)
  섹션2: 선택종목 상세 패널 (우측 사이드)
  섹션3: 자동청산 트리거 현황 (4카드)
  섹션4: 매매 성과 요약 (하단 바)

단독 실행:
    python swing_dashboard.py

외부 호출:
    from swing_dashboard import SwingDashboard
    dash = SwingDashboard(trader=self, db=self.db)
    dash.show()
"""
from __future__ import annotations

import datetime
import os
import sys
from typing import Optional

try:
    from loguru import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QFrame, QGroupBox, QProgressBar, QSplitter,
    QListWidget, QAbstractItemView, QListWidgetItem,
    QMessageBox, QInputDialog, QSystemTrayIcon, QMenu,
    QAction, QShortcut,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QSettings
from PyQt5.QtGui import QColor, QBrush, QKeySequence

# ── 미니차트: pyqtgraph → matplotlib → 텍스트 ────────────────
try:
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=True, background="w")
    _PG_OK = True
except ImportError:
    _PG_OK = False

_MPL_OK = False
if not _PG_OK:
    try:
        import matplotlib
        matplotlib.use("Qt5Agg")
        from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        _MPL_OK = True
    except ImportError:
        pass

_CHAPTER_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_CHAPTER_DIR)
if _CHAPTER_DIR not in sys.path:
    sys.path.insert(0, _CHAPTER_DIR)

try:
    from db_manager import DBManager, DB_PATH
    _DB_OK = True
except ImportError:
    _DB_OK = False


# ──────────────────────────────────────────────────────────────
# 색상 / 상수
# ──────────────────────────────────────────────────────────────

_BG_STRONG_WIN = QColor(200, 230, 201)   # pnl >= +5%
_BG_WIN        = QColor(232, 245, 233)   # +1~5%
_BG_NEUTRAL    = QColor(255, 255, 255)   # -1~+1%
_BG_WARN       = QColor(255, 249, 196)   # -1~-3%
_BG_DANGER     = QColor(255, 235, 238)   # <= -3%

_FG_WIN        = QColor(27,  94,  32)
_FG_LOSE       = QColor(183, 28,  28)
_FG_NEUTRAL    = QColor(66,  66,  66)

_NXT_COLORS = {
    "강력보유": QColor(27,  94,  32),
    "보유유지": QColor(46,  125, 50),
    "재확인":   QColor(245, 127, 23),
    "내일청산": QColor(198, 40,  40),
}

_TABLE_COLS = [
    "순위", "종목명", "업종", "보유일", "진입가",
    "현재가", "수익률", "수급점수", "NXT",
    "익절까지", "손절까지", "액션",
]

_TRIGGER_COOLDOWN = 3600   # 1시간 (초)


# ──────────────────────────────────────────────────────────────
# 공통 헬퍼
# ──────────────────────────────────────────────────────────────

def _cell(text: str, align: int = Qt.AlignCenter, bold: bool = False,
          fg: Optional[QColor] = None,
          bg: Optional[QColor] = None) -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    it.setTextAlignment(align)
    if bold:
        f = it.font(); f.setBold(True); it.setFont(f)
    if fg:
        it.setForeground(QBrush(fg))
    if bg:
        it.setBackground(QBrush(bg))
    return it


def _pnl_bg(pnl: float) -> QColor:
    if pnl >= 5.0:   return _BG_STRONG_WIN
    if pnl >= 1.0:   return _BG_WIN
    if pnl >= -1.0:  return _BG_NEUTRAL
    if pnl >= -3.0:  return _BG_WARN
    return _BG_DANGER


def _pnl_fg(pnl: float) -> QColor:
    if pnl > 0:  return _FG_WIN
    if pnl < 0:  return _FG_LOSE
    return _FG_NEUTRAL


def _hold_days(entry_date: str) -> int:
    try:
        ed = datetime.datetime.strptime(str(entry_date)[:8], "%Y%m%d").date()
        return max(1, (datetime.date.today() - ed).days + 1)
    except Exception:
        return 0


def _nxt_icon(status: str) -> str:
    if "강력보유" in status: return "✅"
    if "보유유지" in status: return "✅"
    if "재확인"   in status: return "⚠️"
    if "내일청산" in status: return "🚨"
    return "–"


# ──────────────────────────────────────────────────────────────
# PriceWorker — 가격 조회 스레드 (읽기 전용 캐시 접근)
# ──────────────────────────────────────────────────────────────

class PriceWorker(QThread):
    """
    trader 실시간 캐시 → DB supply_flow 순으로 현재가 조회.
    Kiwoom COM 직접 호출 없음 (메인 스레드 제약 회피).
    """

    prices_ready = pyqtSignal(dict)   # {code: float}

    def __init__(self, codes: list, trader=None, db=None, parent=None) -> None:
        super().__init__(parent)
        self._codes  = codes
        self._trader = trader
        self._db     = db

    def run(self) -> None:
        prices: dict[str, float] = {}

        if self._trader is not None:
            pd = getattr(self._trader, "stock_code_to_realtime_price_dict", {}) or {}
            for code in self._codes:
                p = pd.get(code) or pd.get(code + "_AL")
                if p:
                    try:
                        prices[code] = float(str(p).replace(",", ""))
                    except Exception:
                        pass

        if self._db:
            for code in self._codes:
                if code in prices:
                    continue
                try:
                    rows = self._db.get_supply_today(code)
                    if rows:
                        p = rows[-1].get("price")
                        if p:
                            prices[code] = float(p)
                except Exception:
                    pass

        self.prices_ready.emit(prices)


# ──────────────────────────────────────────────────────────────
# MiniChart — 수급 추이 미니 라인차트
# ──────────────────────────────────────────────────────────────

if _PG_OK:
    class MiniChart(pg.PlotWidget):
        def __init__(self, parent=None):
            super().__init__(parent, background="white")
            self.setFixedHeight(80)
            self.setMouseEnabled(x=False, y=False)
            self.hideButtons()
            self.showGrid(x=False, y=True, alpha=0.3)
            self.getAxis("bottom").setStyle(showValues=False)
            self.getAxis("left").setStyle(showValues=True)
            self._line = self.plot(pen=pg.mkPen("#1565c0", width=2))
            self._dot  = self.plot(pen=None, symbol="o",
                                   symbolBrush="#1565c0", symbolSize=5)

        def update_scores(self, scores: list) -> None:
            if not scores:
                return
            x = list(range(len(scores)))
            self._line.setData(x, [float(s) for s in scores])
            self._dot.setData(x, [float(s) for s in scores])

elif _MPL_OK:
    class MiniChart(FigureCanvasQTAgg):
        def __init__(self, parent=None):
            self._fig = Figure(figsize=(3, 0.8), dpi=80)
            self._ax  = self._fig.add_subplot(111)
            super().__init__(self._fig)
            self.setFixedHeight(80)
            self._fig.tight_layout(pad=0.2)

        def update_scores(self, scores: list) -> None:
            self._ax.clear()
            if scores:
                self._ax.plot(scores, "b-o", linewidth=1.5, markersize=4)
                self._ax.set_xticks([])
                self._ax.grid(axis="y", alpha=0.3)
            self.draw()

else:
    class MiniChart(QLabel):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setFixedHeight(80)
            self.setAlignment(Qt.AlignCenter)
            self.setStyleSheet(
                "font-size:10px; color:#555; background:#f5f5f5;"
                "border:1px solid #ddd; border-radius:4px;"
            )

        def update_scores(self, scores: list) -> None:
            if not scores:
                self.setText("수급 데이터 없음")
                return
            parts = " → ".join(f"{float(s):.0f}" for s in scores[-7:])
            self.setText(f"점수 추이:\n{parts}")


# ──────────────────────────────────────────────────────────────
# HoldingDetailPanel — 우측 상세 패널
# ──────────────────────────────────────────────────────────────

class HoldingDetailPanel(QWidget):
    half_exit_requested = pyqtSignal(dict)
    full_exit_requested = pyqtSignal(dict)
    memo_requested      = pyqtSignal(dict)

    def __init__(self, db=None, parent=None) -> None:
        super().__init__(parent)
        self._db      = db
        self._holding: Optional[dict] = None
        self._build()

    def _build(self) -> None:
        self.setMinimumWidth(240)
        self.setMaximumWidth(300)
        vl = QVBoxLayout(self)
        vl.setContentsMargins(10, 10, 10, 8)
        vl.setSpacing(6)

        # 헤더
        self._name_lbl = QLabel("종목을 선택하세요")
        self._name_lbl.setStyleSheet(
            "font-size:13px; font-weight:bold; color:#1565c0;"
        )
        self._name_lbl.setWordWrap(True)
        vl.addWidget(self._name_lbl)
        self._sub_lbl = QLabel("")
        self._sub_lbl.setStyleSheet("font-size:11px; color:#555;")
        vl.addWidget(self._sub_lbl)

        # 가격 정보
        price_gb = QGroupBox("가격 정보")
        price_gb.setStyleSheet(
            "QGroupBox { font-size:10px; font-weight:bold; }"
        )
        pvl = QVBoxLayout(price_gb)
        pvl.setSpacing(2)
        self._price_rows: dict[str, QLabel] = {}
        for key in ("진입가", "현재가", "목표가", "손절가"):
            hl = QHBoxLayout()
            k  = QLabel(key)
            k.setFixedWidth(42)
            k.setStyleSheet("font-size:10px; color:#666;")
            hl.addWidget(k)
            v = QLabel("--")
            v.setStyleSheet("font-size:12px; font-weight:bold;")
            hl.addWidget(v)
            hl.addStretch()
            pvl.addLayout(hl)
            self._price_rows[key] = v
        vl.addWidget(price_gb)

        # 수급 현황
        sup_gb = QGroupBox("수급 현황")
        sup_gb.setStyleSheet(
            "QGroupBox { font-size:10px; font-weight:bold; }"
        )
        svl = QVBoxLayout(sup_gb)
        svl.setSpacing(3)
        score_hl = QHBoxLayout()
        sk = QLabel("수급점수")
        sk.setFixedWidth(52)
        sk.setStyleSheet("font-size:10px; color:#666;")
        score_hl.addWidget(sk)
        self._score_lbl = QLabel("--")
        self._score_lbl.setStyleSheet("font-size:11px; font-weight:bold;")
        score_hl.addWidget(self._score_lbl)
        svl.addLayout(score_hl)
        self._score_bar = QProgressBar()
        self._score_bar.setRange(0, 100)
        self._score_bar.setValue(0)
        self._score_bar.setFixedHeight(8)
        self._score_bar.setTextVisible(False)
        self._score_bar.setStyleSheet("""
            QProgressBar { background:#e0e0e0; border-radius:3px; border:none; }
            QProgressBar::chunk { background:#1565c0; border-radius:3px; }
        """)
        svl.addWidget(self._score_bar)
        self._nxt_lbl    = QLabel("NXT상태: --")
        self._nxt_lbl.setStyleSheet("font-size:10px;")
        self._nxtgap_lbl = QLabel("NXT갭:   --")
        self._nxtgap_lbl.setStyleSheet("font-size:10px;")
        svl.addWidget(self._nxt_lbl)
        svl.addWidget(self._nxtgap_lbl)
        vl.addWidget(sup_gb)

        # 미니차트
        chart_gb = QGroupBox("수급점수 추이")
        chart_gb.setStyleSheet(
            "QGroupBox { font-size:10px; font-weight:bold; }"
        )
        cvl = QVBoxLayout(chart_gb)
        cvl.setContentsMargins(4, 4, 4, 4)
        self._mini_chart = MiniChart()
        cvl.addWidget(self._mini_chart)
        vl.addWidget(chart_gb)

        # 청산 시나리오
        scen_gb = QGroupBox("청산 시나리오")
        scen_gb.setStyleSheet(
            "QGroupBox { font-size:10px; font-weight:bold; }"
        )
        scen_vl = QVBoxLayout(scen_gb)
        self._scenario_lbl = QLabel("종목을 선택하면\n시나리오가 표시됩니다.")
        self._scenario_lbl.setStyleSheet("font-size:10px; color:#333;")
        self._scenario_lbl.setWordWrap(True)
        scen_vl.addWidget(self._scenario_lbl)
        vl.addWidget(scen_gb)

        # 메모
        self._memo_lbl = QLabel("")
        self._memo_lbl.setStyleSheet(
            "font-size:10px; color:#777; font-style:italic;"
        )
        self._memo_lbl.setWordWrap(True)
        vl.addWidget(self._memo_lbl)

        # 버튼 바
        btn_hl = QHBoxLayout()
        self._half_btn = QPushButton("절반익절")
        self._half_btn.setFixedHeight(26)
        self._half_btn.setStyleSheet(
            "background:#2e7d32; color:white; border-radius:3px; font-size:11px;"
        )
        self._half_btn.clicked.connect(
            lambda: self._holding and self.half_exit_requested.emit(self._holding)
        )
        btn_hl.addWidget(self._half_btn)
        self._full_btn = QPushButton("전량청산")
        self._full_btn.setFixedHeight(26)
        self._full_btn.setStyleSheet(
            "background:#c62828; color:white; border-radius:3px; font-size:11px;"
        )
        self._full_btn.clicked.connect(
            lambda: self._holding and self.full_exit_requested.emit(self._holding)
        )
        btn_hl.addWidget(self._full_btn)
        self._memo_btn = QPushButton("메모")
        self._memo_btn.setFixedSize(46, 26)
        self._memo_btn.setStyleSheet(
            "background:#546e7a; color:white; border-radius:3px; font-size:11px;"
        )
        self._memo_btn.clicked.connect(
            lambda: self._holding and self.memo_requested.emit(self._holding)
        )
        btn_hl.addWidget(self._memo_btn)
        vl.addLayout(btn_hl)
        vl.addStretch()

    # ── 업데이트 ─────────────────────────────────────────────

    def update(self, h: dict) -> None:
        self._holding = h
        self._update_basic(h)
        self._update_supply(h)
        self._update_chart(h)
        self._update_scenario(h)

    def _update_basic(self, h: dict) -> None:
        name    = str(h.get("name") or h.get("code", ""))
        code    = str(h.get("code", ""))
        sec     = str(h.get("sector") or "")
        days    = int(h.get("hold_days") or 0)
        entry   = float(h.get("entry_price")   or 0)
        current = float(h.get("current_price") or entry)
        pnl_pct = float(h.get("pnl_pct")      or 0)
        target  = entry * 1.05
        stop    = entry * 0.97

        self._name_lbl.setText(f"{name}  ({code})")
        self._sub_lbl.setText(
            f"{sec}  보유 {days}일째" if sec else f"보유 {days}일째"
        )

        fg_pnl = _pnl_fg(pnl_pct)
        target_tag = "  ✓ 달성!" if current >= target else ""
        stop_tag   = "  ⚠ 임박"  if current <= stop   else ""

        self._price_rows["진입가"].setText(f"{entry:,.0f}원")
        self._price_rows["진입가"].setStyleSheet(
            "font-size:12px; font-weight:bold;"
        )
        self._price_rows["현재가"].setText(
            f"{current:,.0f}원  {pnl_pct:+.2f}%"
        )
        self._price_rows["현재가"].setStyleSheet(
            f"font-size:12px; font-weight:bold; color:{fg_pnl.name()};"
        )
        self._price_rows["목표가"].setText(f"{target:,.0f}원{target_tag}")
        self._price_rows["손절가"].setText(f"{stop:,.0f}원{stop_tag}")

    def _update_supply(self, h: dict) -> None:
        score = int(float(h.get("score") or 0))
        nxt   = str(h.get("nxt_status") or "--")
        icon  = _nxt_icon(nxt)
        color = _NXT_COLORS.get(
            next((k for k in _NXT_COLORS if k in nxt), ""), QColor("#333333")
        )
        self._score_lbl.setText(f"{score}점")
        self._score_bar.setValue(score)
        self._nxt_lbl.setText(f"NXT상태:  {icon} {nxt}")
        self._nxt_lbl.setStyleSheet(
            f"font-size:10px; font-weight:bold; color:{color.name()};"
        )

        gap_text = "--"
        if self._db:
            try:
                hist = self._db.get_nxt_history(str(h.get("code", "")))
                if hist:
                    gap = float(hist[0].get("nxt_gap") or 0)
                    gap_text = (
                        f"{gap/100:+.0f}억 추가매수" if gap > 0 else f"{gap/100:+.0f}억"
                    )
            except Exception:
                pass
        self._nxtgap_lbl.setText(f"NXT갭:    {gap_text}")

        memo = str(h.get("memo") or "")
        self._memo_lbl.setText(f"📝 {memo}" if memo else "")

    def _update_chart(self, h: dict) -> None:
        if self._db is None:
            return
        try:
            rows   = self._db.get_supply_today(str(h.get("code", "")))
            scores = [
                float(r.get("foreigner_buy") or 0)
                for r in rows if r.get("foreigner_buy") is not None
            ]
            self._mini_chart.update_scores(scores[-10:])
        except Exception:
            pass

    def _update_scenario(self, h: dict) -> None:
        pnl  = float(h.get("pnl_pct")  or 0)
        days = int(h.get("hold_days")  or 0)
        nxt  = str(h.get("nxt_status") or "")
        parts: list[str] = []
        if pnl >= 5.0:
            parts.append("① +5% 달성 → 절반 익절 권고")
        elif pnl >= 3.0:
            parts.append("① 목표가 근접 → 절반 익절 고려")
        if "내일청산" in nxt:
            parts.append("② NXT 이탈 → 내일 동시호가 청산")
        elif "재확인" in nxt:
            parts.append("② NXT 재확인 → 내일 오전 수급 체크")
        if days >= 4:
            parts.append(f"③ 보유 {days}일 → 내일 강제청산 검토")
        if not parts:
            parts.append("현재 청산 시나리오 없음\n정상 보유 중")
        self._scenario_lbl.setText("\n".join(parts))

    def clear(self) -> None:
        self._holding = None
        self._name_lbl.setText("종목을 선택하세요")
        self._sub_lbl.setText("")
        for lbl in self._price_rows.values():
            lbl.setText("--")
        self._score_lbl.setText("--")
        self._score_bar.setValue(0)
        self._nxt_lbl.setText("NXT상태: --")
        self._nxtgap_lbl.setText("NXT갭:   --")
        self._scenario_lbl.setText("종목을 선택하면\n시나리오가 표시됩니다.")
        self._memo_lbl.setText("")


# ──────────────────────────────────────────────────────────────
# TriggerCard — 트리거 카드 (단일)
# ──────────────────────────────────────────────────────────────

class TriggerCard(QFrame):
    def __init__(self, title: str, subtitle: str,
                 condition: str, color: str = "#c62828",
                 parent=None) -> None:
        super().__init__(parent)
        self._color       = color
        self._idle_style  = (
            "QFrame { background:white; border:1px solid #ccc;"
            "border-radius:8px; }"
        )
        self._active_style = (
            f"QFrame {{ background:#fff8f8; border:2px solid {color};"
            "border-radius:8px; }"
        )
        self.setStyleSheet(self._idle_style)
        self.setMinimumWidth(150)
        vl = QVBoxLayout(self)
        vl.setContentsMargins(10, 8, 10, 8)
        vl.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet(f"font-size:12px; font-weight:bold; color:{color};")
        vl.addWidget(t)
        s = QLabel(subtitle)
        s.setStyleSheet("font-size:10px; color:#555;")
        vl.addWidget(s)
        c = QLabel(condition)
        c.setStyleSheet("font-size:10px; color:#888;")
        vl.addWidget(c)
        self._stock_lbl = QLabel("해당없음")
        self._stock_lbl.setStyleSheet(
            "font-size:11px; color:#bbb; margin-top:4px;"
        )
        self._stock_lbl.setWordWrap(True)
        vl.addWidget(self._stock_lbl)

    def update_targets(self, targets: list[dict]) -> None:
        if targets:
            self.setStyleSheet(self._active_style)
            names  = ", ".join(
                str(t.get("name") or t.get("code", "")) for t in targets[:3]
            )
            suffix = f" 외 {len(targets)-3}종목" if len(targets) > 3 else ""
            self._stock_lbl.setText(names + suffix)
            self._stock_lbl.setStyleSheet(
                f"font-size:11px; font-weight:bold; color:{self._color};"
                "margin-top:4px;"
            )
        else:
            self.setStyleSheet(self._idle_style)
            self._stock_lbl.setText("해당없음")
            self._stock_lbl.setStyleSheet(
                "font-size:11px; color:#bbb; margin-top:4px;"
            )


# ──────────────────────────────────────────────────────────────
# TriggerMonitor — 4카드 트리거 모니터
# ──────────────────────────────────────────────────────────────

class TriggerMonitor(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        gb   = QGroupBox("🔔 자동청산 트리거 모니터")
        gb.setStyleSheet("QGroupBox { font-size:12px; font-weight:bold; }")
        gb_hl = QHBoxLayout(gb)
        gb_hl.setSpacing(8)

        self._cards: dict[str, TriggerCard] = {
            "익절": TriggerCard(
                "익절 트리거", "+5% 도달 → 절반 청산",
                "수익률 ≥ +5.0%", "#2e7d32"
            ),
            "손절": TriggerCard(
                "손절 트리거", "-3% 도달 → 전량 청산",
                "수익률 ≤ -3.0%", "#c62828"
            ),
            "NXT": TriggerCard(
                "NXT 청산 트리거", "NXT 30% 이탈 → 내일청산",
                "nxt_status = 내일청산", "#c62828"
            ),
            "기간": TriggerCard(
                "기간 트리거", "5거래일 초과 → 강제청산",
                "보유일 ≥ 5일", "#e65100"
            ),
        }
        for card in self._cards.values():
            gb_hl.addWidget(card)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(gb)

    def refresh(self, holdings: list[dict]) -> None:
        self._cards["익절"].update_targets(
            [h for h in holdings if float(h.get("pnl_pct") or 0) >= 5.0]
        )
        self._cards["손절"].update_targets(
            [h for h in holdings if float(h.get("pnl_pct") or 0) <= -3.0]
        )
        self._cards["NXT"].update_targets(
            [h for h in holdings
             if "내일청산" in str(h.get("nxt_status") or "")]
        )
        self._cards["기간"].update_targets(
            [h for h in holdings if int(h.get("hold_days") or 0) >= 5]
        )


# ──────────────────────────────────────────────────────────────
# PerformanceWidget — 매매 성과 하단 바
# ──────────────────────────────────────────────────────────────

class PerformanceWidget(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(72)
        self.setStyleSheet(
            "QFrame { background:#f5f5f5; border-top:1px solid #ddd; }"
        )
        self._build()

    def _build(self) -> None:
        hl = QHBoxLayout(self)
        hl.setContentsMargins(16, 6, 16, 6)
        hl.setSpacing(20)

        for attr, title in [
            ("_month_box", "이번달 성과"),
            ("_strat_box", "전략별 성과"),
            ("_week_box",  "이번주 PnL"),
        ]:
            gb = QGroupBox(title)
            gb.setStyleSheet(
                "QGroupBox { font-size:10px; font-weight:bold; border:none; }"
            )
            gvl = QVBoxLayout(gb)
            gvl.setContentsMargins(4, 0, 4, 0)
            gvl.setSpacing(1)
            lbl1 = QLabel("--")
            lbl1.setStyleSheet("font-size:11px;")
            gvl.addWidget(lbl1)
            lbl2 = QLabel("")
            lbl2.setStyleSheet("font-size:11px;")
            gvl.addWidget(lbl2)
            setattr(self, attr + "_lbl1", lbl1)
            setattr(self, attr + "_lbl2", lbl2)
            hl.addWidget(gb)

        hl.addStretch()

    def update_performance(self, perf: dict) -> None:
        total = perf.get("total", 0)
        win   = perf.get("win",   0)
        lose  = perf.get("lose",  0)
        wr    = perf.get("win_rate", 0.0)
        aw    = perf.get("avg_win",  0.0)
        al    = perf.get("avg_loss", 0.0)
        ev    = perf.get("expectancy", 0.0)
        self._month_box_lbl1.setText(
            f"총 {total}회  승 {win}  패 {lose}  승률 {wr:.1f}%"
        )
        self._month_box_lbl2.setText(
            f"평균수익 {aw:+.1f}%  평균손실 {al:+.1f}%  기대값 {ev:+.2f}%"
        )
        jw  = perf.get("jongbe_wr",   0.0)
        sw  = perf.get("swing_wr",    0.0)
        jd  = perf.get("jongbe_days", 0.0)
        sd  = perf.get("swing_days",  0.0)
        self._strat_box_lbl1.setText(
            f"종베: 승률 {jw:.0f}%  /  평균 {jd:.1f}일"
        )
        self._strat_box_lbl2.setText(
            f"스윙: 승률 {sw:.0f}%  /  평균 {sd:.1f}일"
        )

    def update_week_pnl(self, week_data: list[tuple]) -> None:
        day_order = ["월", "화", "수", "목", "금"]
        day_map   = dict(week_data)
        parts     = [
            f"{d}: {day_map[d]:+.1f}%" if d in day_map else f"{d}: --"
            for d in day_order
        ]
        self._week_box_lbl1.setText("  ".join(parts[:3]))
        self._week_box_lbl2.setText("  ".join(parts[3:]))


# ──────────────────────────────────────────────────────────────
# SwingDashboard — 메인 대시보드
# ──────────────────────────────────────────────────────────────

class SwingDashboard(QMainWindow):
    """
    스윙 보유종목 대시보드.

    단독: python swing_dashboard.py
    외부: SwingDashboard(trader=self, db=self.db).show()
    """

    def __init__(self, trader=None, db=None, parent=None) -> None:
        super().__init__(parent)
        self._trader  = trader
        self.db: DBManager = db or (
            getattr(trader, "db", None) if trader else (DBManager() if _DB_OK else None)
        )
        self.holdings: list[dict] = []
        self._sort_col  = -1
        self._sort_asc  = True
        self._live      = True
        self._price_worker: Optional[PriceWorker] = None
        self._trigger_last: dict[tuple, datetime.datetime] = {}

        self._migrate_memo()
        self._setup_ui()
        self._setup_tray()
        self._setup_shortcuts()
        self._setup_timers()
        self._restore_settings()
        self.load_holdings()

    # ── DB 마이그레이션 ──────────────────────────────────────

    def _migrate_memo(self) -> None:
        if not self.db:
            return
        try:
            cur = self.db._conn.cursor()
            cur.execute("PRAGMA table_info(holding)")
            cols = {r[1] for r in cur.fetchall()}
            if "memo" not in cols:
                cur.execute("ALTER TABLE holding ADD COLUMN memo TEXT")
                self.db._conn.commit()
                logger.info("[SwingDash] holding.memo 컬럼 추가")
        except Exception as e:
            logger.warning(f"[SwingDash] memo 마이그레이션 실패: {e}")

    def _patch_holding(self, code: str, **kwargs) -> None:
        """holding 테이블 부분 업데이트 (기존 값 유지)."""
        if not self.db or not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [code]
        try:
            self.db._conn.execute(
                f"UPDATE holding SET {sets} WHERE code = ?", vals
            )
            self.db._conn.commit()
        except Exception as e:
            logger.warning(f"[SwingDash] patch_holding 실패: {e}")

    # ── UI 구성 ──────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setWindowTitle("📈 스윙 보유종목 대시보드")
        self.setMinimumSize(1100, 700)

        central = QWidget()
        self.setCentralWidget(central)
        mvl = QVBoxLayout(central)
        mvl.setContentsMargins(0, 0, 0, 0)
        mvl.setSpacing(0)

        mvl.addWidget(self._build_header())

        # 메인 스플리터 (좌: 테이블+트리거  /  우: 상세)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(2)
        splitter.setStyleSheet("QSplitter::handle { background:#e0e0e0; }")

        left = QWidget()
        lvl  = QVBoxLayout(left)
        lvl.setContentsMargins(8, 8, 4, 4)
        lvl.setSpacing(8)
        lvl.addWidget(self._build_table(), stretch=1)
        self._trigger_mon = TriggerMonitor()
        lvl.addWidget(self._trigger_mon)
        splitter.addWidget(left)

        self._detail = HoldingDetailPanel(db=self.db)
        self._detail.half_exit_requested.connect(
            lambda h: self.close_holding(h["code"], "절반익절", half=True)
        )
        self._detail.full_exit_requested.connect(
            lambda h: self.close_holding(h["code"], "전량청산")
        )
        self._detail.memo_requested.connect(self._on_memo)
        splitter.addWidget(self._detail)
        splitter.setSizes([820, 280])

        mvl.addWidget(splitter, stretch=1)

        self._perf_widget = PerformanceWidget()
        mvl.addWidget(self._perf_widget)

    def _build_header(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(56)
        bar.setStyleSheet("background:#1565c0;")
        hl  = QHBoxLayout(bar)
        hl.setContentsMargins(16, 6, 16, 6)

        ttl = QLabel("📈 스윙 보유종목 대시보드")
        ttl.setStyleSheet("color:white; font-size:14px; font-weight:bold;")
        hl.addWidget(ttl)
        hl.addSpacing(20)

        self._hdr_total = QLabel("총평가금액: --")
        self._hdr_total.setStyleSheet("color:white; font-size:12px;")
        hl.addWidget(self._hdr_total)
        hl.addSpacing(10)

        self._hdr_ret = QLabel("총수익률: --")
        self._hdr_ret.setStyleSheet("color:#a5d6a7; font-size:12px; font-weight:bold;")
        hl.addWidget(self._hdr_ret)
        hl.addSpacing(10)

        self._hdr_summary = QLabel("")
        self._hdr_summary.setStyleSheet("color:#bbdefb; font-size:11px;")
        hl.addWidget(self._hdr_summary)
        hl.addStretch()

        self._live_btn = QPushButton("실시간갱신 ON")
        self._live_btn.setCheckable(True)
        self._live_btn.setChecked(True)
        self._live_btn.setFixedSize(110, 26)
        self._live_btn.setStyleSheet("""
            QPushButton          { background:#2e7d32; color:white;
                                   border-radius:4px; font-size:11px; font-weight:bold; }
            QPushButton:!checked { background:#546e7a; }
        """)
        self._live_btn.clicked.connect(self._toggle_live)
        hl.addWidget(self._live_btn)

        ref_btn = QPushButton("새로고침 (F5)")
        ref_btn.setFixedSize(100, 26)
        ref_btn.setStyleSheet(
            "background:#1976d2; color:white; border-radius:4px; font-size:11px;"
        )
        ref_btn.clicked.connect(self.refresh_all)
        hl.addWidget(ref_btn)
        return bar

    def _build_table(self) -> QWidget:
        wrap = QWidget()
        vl   = QVBoxLayout(wrap)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(4)

        lbl = QLabel("보유종목")
        lbl.setStyleSheet("font-size:12px; font-weight:bold; color:#333;")
        vl.addWidget(lbl)

        self._table = QTableWidget(0, len(_TABLE_COLS))
        self._table.setHorizontalHeaderLabels(_TABLE_COLS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(False)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(48)
        self._table.setShowGrid(True)
        self._table.setStyleSheet("""
            QTableWidget           { gridline-color:#e0e0e0; }
            QTableWidget::item:selected { background:#bbdefb; color:#000; }
        """)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        for i in range(3, len(_TABLE_COLS) - 1):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(len(_TABLE_COLS) - 1, QHeaderView.Fixed)
        self._table.setColumnWidth(len(_TABLE_COLS) - 1, 96)

        hdr.sectionDoubleClicked.connect(self._on_header_dbl)
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        vl.addWidget(self._table)
        return wrap

    # ── 타이머 ───────────────────────────────────────────────

    def _setup_timers(self) -> None:
        self._price_timer = QTimer(self)
        self._price_timer.timeout.connect(self.refresh_prices)
        self._price_timer.start(60_000)

        self._trigger_timer = QTimer(self)
        self._trigger_timer.timeout.connect(self.check_triggers)
        self._trigger_timer.start(30_000)

        self._perf_timer = QTimer(self)
        self._perf_timer.timeout.connect(self._refresh_performance)
        self._perf_timer.start(300_000)

    def _toggle_live(self, checked: bool) -> None:
        self._live = checked
        self._live_btn.setText(f"실시간갱신 {'ON' if checked else 'OFF'}")
        if checked:
            self._price_timer.start(60_000)
        else:
            self._price_timer.stop()

    # ── 시스템 트레이 ────────────────────────────────────────

    def _setup_tray(self) -> None:
        self._tray: Optional[QSystemTrayIcon] = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(self.style().standardIcon(self.style().SP_ComputerIcon))
        self._tray.setToolTip("스윙 보유종목 대시보드")
        menu     = QMenu()
        show_act = QAction("대시보드 열기", self)
        show_act.triggered.connect(self._show_raise)
        quit_act = QAction("종료", self)
        quit_act.triggered.connect(QApplication.quit)
        menu.addAction(show_act)
        menu.addSeparator()
        menu.addAction(quit_act)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(
            lambda r: self._show_raise()
            if r == QSystemTrayIcon.DoubleClick else None
        )
        self._tray.show()

    def _show_raise(self) -> None:
        self.show(); self.raise_(); self.activateWindow()

    def _notify(self, title: str, msg: str) -> None:
        if self._tray and QSystemTrayIcon.supportsMessages():
            self._tray.showMessage(title, msg, QSystemTrayIcon.Warning, 4000)

    # ── 단축키 ───────────────────────────────────────────────

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("F5"),       self).activated.connect(self.refresh_all)
        QShortcut(QKeySequence("F1"),       self).activated.connect(
            lambda: self._detail.raise_()
        )
        QShortcut(QKeySequence("Ctrl+E"),   self).activated.connect(
            lambda: self._selected() and self.close_holding(
                self._selected()["code"], "전량청산"
            )
        )
        QShortcut(QKeySequence("Ctrl+M"),   self).activated.connect(
            lambda: self._selected() and self._on_memo(self._selected())
        )
        QShortcut(QKeySequence("Ctrl+R"),   self).activated.connect(
            self._show_performance_report
        )

    # ── 데이터 로드 ──────────────────────────────────────────

    def load_holdings(self) -> None:
        if not self.db:
            return
        try:
            all_h = self.db.get_holding_list()
            self.holdings = [
                h for h in all_h if h.get("status") != "청산"
            ]
            for h in self.holdings:
                h["hold_days"] = _hold_days(str(h.get("entry_date") or ""))
        except Exception as e:
            logger.warning(f"[SwingDash] load_holdings 실패: {e}")
            self.holdings = []
        self.refresh_table()
        self._refresh_header()
        self._trigger_mon.refresh(self.holdings)
        self._refresh_performance()

    # ── 가격 갱신 ────────────────────────────────────────────

    def refresh_prices(self) -> None:
        if not self.holdings or not self.db:
            return
        if self._price_worker and self._price_worker.isRunning():
            return
        codes = [str(h["code"]) for h in self.holdings]
        self._price_worker = PriceWorker(codes, self._trader, self.db)
        self._price_worker.prices_ready.connect(self._on_prices_ready)
        self._price_worker.start()

    def _on_prices_ready(self, prices: dict) -> None:
        changed = False
        for h in self.holdings:
            code  = str(h.get("code", ""))
            price = prices.get(code)
            if price is None:
                continue
            entry = float(h.get("entry_price") or price)
            pnl   = round((price - entry) / entry * 100, 2) if entry else 0.0
            h["current_price"] = price
            h["pnl_pct"]       = pnl
            self._patch_holding(code,
                                current_price=price,
                                pnl_pct=pnl,
                                hold_days=h.get("hold_days", 0))
            changed = True
        if changed:
            self.refresh_table()
            self._refresh_header()
            self._trigger_mon.refresh(self.holdings)

    # ── 트리거 체크 ──────────────────────────────────────────

    def check_triggers(self) -> None:
        now = datetime.datetime.now()
        for h in self.holdings:
            pnl  = float(h.get("pnl_pct")  or 0)
            nxt  = str(h.get("nxt_status") or "")
            days = int(h.get("hold_days")  or 0)
            name = str(h.get("name") or h.get("code", ""))

            if pnl >= 5.0:
                self._maybe_trigger(
                    h, now, "익절",
                    f"🟢 익절 트리거\n{name}  +{pnl:.1f}%\n절반 익절 권고"
                )
            if pnl <= -3.0:
                self._maybe_trigger(
                    h, now, "손절",
                    f"🔴 손절 트리거\n{name}  {pnl:.1f}%\n즉시 청산 권고"
                )
            if "내일청산" in nxt:
                self._maybe_trigger(
                    h, now, "NXT청산",
                    f"🚨 NXT 청산 트리거\n{name}\nNXT 외인 이탈! 내일 동시호가 청산"
                )
            if days >= 5:
                self._maybe_trigger(
                    h, now, "기간초과",
                    f"⏰ 기간 트리거\n{name}\n보유 {days}일 초과! 강제청산 검토"
                )
        self._trigger_mon.refresh(self.holdings)

    def _maybe_trigger(self, h: dict, now: datetime.datetime,
                       ttype: str, msg: str) -> None:
        key  = (str(h.get("code")), ttype)
        last = self._trigger_last.get(key)
        if last and (now - last).total_seconds() < _TRIGGER_COOLDOWN:
            return
        self._trigger_last[key] = now
        self._notify(f"⚡ 트리거: {h.get('name')}", msg)
        logger.info(f"[SwingDash] 트리거 {ttype}: {h.get('code')}")

    # ── 테이블 갱신 ──────────────────────────────────────────

    def refresh_table(self) -> None:
        holdings = list(self.holdings)
        if self._sort_col >= 0:
            holdings = self._apply_sort(holdings)
        else:
            holdings.sort(key=lambda h: float(h.get("pnl_pct") or 0), reverse=True)

        self._table.setRowCount(0)
        for rank, h in enumerate(holdings, 1):
            self._insert_row(rank, h)

    def _insert_row(self, rank: int, h: dict) -> None:
        r     = self._table.rowCount()
        self._table.insertRow(r)

        pnl   = float(h.get("pnl_pct")      or 0)
        entry = float(h.get("entry_price")   or 0)
        cur   = float(h.get("current_price") or entry)
        score = int(float(h.get("score")     or 0))
        days  = int(h.get("hold_days")       or 0)
        nxt   = str(h.get("nxt_status")      or "--")
        name  = str(h.get("name")            or h.get("code", ""))
        sec   = str(h.get("sector")          or "--")

        target    = entry * 1.05
        stop      = entry * 0.97
        to_target = (target - cur) / cur * 100 if cur else 0.0
        to_stop   = (cur  - stop)  / cur * 100 if cur else 0.0

        bg     = _pnl_bg(pnl)
        fg_pnl = _pnl_fg(pnl)
        nxt_fg = _NXT_COLORS.get(
            next((k for k in _NXT_COLORS if k in nxt), ""), QColor("#333333")
        )
        icon = _nxt_icon(nxt)

        def cb(text, align=Qt.AlignCenter, bold=False,
               fg=None) -> QTableWidgetItem:
            return _cell(text, align, bold, fg=fg, bg=bg)

        self._table.setItem(r, 0,  cb(str(rank)))
        it_name = cb(name, Qt.AlignLeft | Qt.AlignVCenter, bold=True)
        it_name.setData(Qt.UserRole, h.get("code"))
        self._table.setItem(r, 1,  it_name)
        self._table.setItem(r, 2,  cb(sec))
        self._table.setItem(r, 3,  cb(f"{days}일"))
        self._table.setItem(r, 4,  cb(f"{entry:,.0f}"))
        self._table.setItem(r, 5,  cb(f"{cur:,.0f}", bold=True))
        self._table.setItem(r, 6,  cb(f"{pnl:+.2f}%", bold=True, fg=fg_pnl))
        self._table.setItem(r, 7,  cb(f"{score}점"))
        self._table.setItem(r, 8,  cb(f"{icon} {nxt}", fg=nxt_fg))
        self._table.setItem(r, 9,  cb(f"{to_target:+.2f}%", fg=_FG_WIN))
        self._table.setItem(r, 10, cb(f"{to_stop:+.2f}%",   fg=_FG_LOSE))
        self._table.setCellWidget(r, 11, self._make_action_btn(h, pnl, nxt, days))

    def _make_action_btn(self, h: dict, pnl: float,
                         nxt: str, days: int) -> QPushButton:
        code = str(h.get("code", ""))
        if pnl >= 5.0:
            btn = QPushButton("절반익절")
            btn.setStyleSheet(
                "background:#2e7d32; color:white; border-radius:3px; font-size:11px;"
            )
            btn.clicked.connect(
                lambda _, c=code: self.close_holding(c, "절반익절", half=True)
            )
        elif "내일청산" in nxt:
            btn = QPushButton("내일청산")
            btn.setStyleSheet(
                "background:#c62828; color:white; border-radius:3px; font-size:11px;"
            )
            btn.clicked.connect(
                lambda _, c=code: self.close_holding(c, "NXT청산")
            )
        elif days >= 5:
            btn = QPushButton("강제청산")
            btn.setStyleSheet(
                "background:#e65100; color:white; border-radius:3px; font-size:11px;"
            )
            btn.clicked.connect(
                lambda _, c=code: self.close_holding(c, "기간초과청산")
            )
        else:
            btn = QPushButton("상세보기")
            btn.setStyleSheet(
                "background:#546e7a; color:white; border-radius:3px; font-size:11px;"
            )
            btn.clicked.connect(
                lambda _, c=code: self._show_detail(c)
            )
        btn.setFixedHeight(32)
        return btn

    # ── 정렬 ────────────────────────────────────────────────

    def _on_header_dbl(self, col: int) -> None:
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        self.refresh_table()

    def _apply_sort(self, holdings: list[dict]) -> list[dict]:
        _KEY_MAP = [
            None, "name", "sector", "hold_days", "entry_price",
            "current_price", "pnl_pct", "score", "nxt_status",
            None, None, None,
        ]
        key = _KEY_MAP[self._sort_col] if self._sort_col < len(_KEY_MAP) else None
        if key is None:
            return holdings
        return sorted(holdings,
                      key=lambda h: (h.get(key) or 0),
                      reverse=not self._sort_asc)

    # ── 선택 감지 ────────────────────────────────────────────

    def _on_row_selected(self) -> None:
        h = self._selected()
        if h:
            self._detail.update(h)

    def _selected(self) -> Optional[dict]:
        row = self._table.currentRow()
        if row < 0:
            return None
        it = self._table.item(row, 1)
        if it is None:
            return None
        code = it.data(Qt.UserRole)
        return next((h for h in self.holdings if h.get("code") == code), None)

    def _show_detail(self, code: str) -> None:
        h = next((h for h in self.holdings if h.get("code") == code), None)
        if h:
            self._detail.update(h)

    # ── 헤더 통계 ────────────────────────────────────────────

    def _refresh_header(self) -> None:
        n = len(self.holdings)
        if n == 0:
            self._hdr_total.setText("총평가금액: --")
            self._hdr_ret.setText("총수익률: --")
            self._hdr_summary.setText("보유: 0종목")
            return
        avg_pnl   = sum(float(h.get("pnl_pct") or 0) for h in self.holdings) / n
        total_val = sum(
            float(h.get("current_price") or h.get("entry_price") or 0)
            for h in self.holdings
        )
        익절대기 = sum(1 for h in self.holdings if float(h.get("pnl_pct") or 0) >= 5.0)
        청산예정 = sum(
            1 for h in self.holdings
            if "내일청산" in str(h.get("nxt_status") or "")
        )
        self._hdr_total.setText(f"총평가금액: {total_val:,.0f}원")
        color = "#a5d6a7" if avg_pnl >= 0 else "#ef9a9a"
        self._hdr_ret.setText(f"총수익률: {avg_pnl:+.2f}%")
        self._hdr_ret.setStyleSheet(
            f"color:{color}; font-size:12px; font-weight:bold;"
        )
        self._hdr_summary.setText(
            f"보유: {n}종목  익절대기: {익절대기}  청산예정: {청산예정}"
        )

    # ── 보유종목 관리 ────────────────────────────────────────

    def add_holding(self, scan_result: dict) -> None:
        """종베 스캐너에서 진입 확정 시 호출."""
        if not self.db:
            return
        today = datetime.date.today().strftime("%Y%m%d")
        code  = str(scan_result.get("code", ""))
        self.db.update_holding(code, {
            "name":          scan_result.get("name", ""),
            "sector":        scan_result.get("sector", ""),
            "status":        "보유",
            "entry_date":    today,
            "entry_price":   scan_result.get("entry"),
            "current_price": scan_result.get("entry"),
            "pnl_pct":       0.0,
            "score":         scan_result.get("score", 0),
            "nxt_status":    "--",
            "hold_days":     1,
        })
        self.load_holdings()
        logger.info(f"[SwingDash] add_holding: {code}")

    def close_holding(self, code: str, reason: str,
                      half: bool = False) -> None:
        """청산 처리 — trade_log 저장 + holding 제거."""
        h = next((h for h in self.holdings if h.get("code") == code), None)
        if h is None:
            return
        name  = str(h.get("name") or code)
        label = "절반 익절" if half else "전량 청산"
        ans   = QMessageBox.question(
            self, f"{label} 확인",
            f"{name}  {label} 처리하겠습니까?\n사유: {reason}",
            QMessageBox.Yes | QMessageBox.No
        )
        if ans != QMessageBox.Yes:
            return

        today   = datetime.date.today().strftime("%Y%m%d")
        cur_p   = float(h.get("current_price") or h.get("entry_price") or 0)
        entry_p = float(h.get("entry_price") or cur_p)
        pnl     = float(h.get("pnl_pct") or 0)

        if self.db:
            try:
                self.db.save_trade({
                    "date":        today,
                    "code":        code,
                    "name":        h.get("name"),
                    "entry_price": entry_p,
                    "entry_time":  str(h.get("entry_date") or ""),
                    "exit_price":  cur_p,
                    "exit_time":   datetime.datetime.now().strftime("%Y%m%d %H%M%S"),
                    "pnl_pct":     pnl,
                    "hold_days":   h.get("hold_days"),
                    "exit_reason": reason,
                    "score":       h.get("score"),
                    "entry_cond":  {},
                })
            except Exception as e:
                logger.warning(f"[SwingDash] save_trade 실패: {e}")

            if half:
                self._patch_holding(
                    code, memo=f"절반익절 완료 ({today})"
                )
            else:
                try:
                    self.db.remove_holding(code)
                except Exception as e:
                    logger.warning(f"[SwingDash] remove_holding 실패: {e}")

        self.load_holdings()
        self._detail.clear()
        logger.info(f"[SwingDash] close_holding: {code}  reason={reason}  half={half}")

    # ── 메모 ────────────────────────────────────────────────

    def _on_memo(self, h: dict) -> None:
        code = str(h.get("code", ""))
        name = str(h.get("name") or code)
        old  = str(h.get("memo") or "")
        text, ok = QInputDialog.getMultiLineText(
            self, f"메모 — {name}", "메모 내용:", old
        )
        if not ok:
            return
        self._patch_holding(code, memo=text)
        h["memo"] = text
        self._detail.update(h)

    # ── 성과 ────────────────────────────────────────────────

    def _refresh_performance(self) -> None:
        if not self.db:
            return
        try:
            perf = self.calc_performance()
            self._perf_widget.update_performance(perf)
            self._perf_widget.update_week_pnl(self._calc_week_pnl())
        except Exception as e:
            logger.debug(f"[SwingDash] 성과 갱신 실패: {e}")

    def calc_performance(self) -> dict:
        if not self.db:
            return {}
        trades  = self.db.get_trade_history(days=30)
        if not trades:
            return {
                "total": 0, "win": 0, "lose": 0,
                "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "expectancy": 0.0, "jongbe_wr": 0.0, "swing_wr": 0.0,
                "jongbe_days": 0.0, "swing_days": 0.0,
            }
        wins   = [t for t in trades if float(t.get("pnl_pct") or 0) >  0]
        losses = [t for t in trades if float(t.get("pnl_pct") or 0) <= 0]
        wr     = len(wins) / len(trades)
        avg_w  = (sum(float(t["pnl_pct"]) for t in wins)   / len(wins))   if wins   else 0.0
        avg_l  = (sum(float(t["pnl_pct"]) for t in losses) / len(losses)) if losses else 0.0
        ev     = wr * avg_w + (1 - wr) * avg_l

        jb     = [t for t in trades
                  if any(kw in str(t.get("exit_reason") or "")
                         for kw in ("종베", "절반", "NXT"))]
        sw     = [t for t in trades if t not in jb]
        _wr    = lambda lst: (
            sum(1 for t in lst if float(t.get("pnl_pct") or 0) > 0) / len(lst) * 100
        ) if lst else 0.0
        _days  = lambda lst: (
            sum(int(t.get("hold_days") or 0) for t in lst) / len(lst)
        ) if lst else 0.0

        return {
            "total": len(trades), "win": len(wins), "lose": len(losses),
            "win_rate": wr * 100, "avg_win": avg_w, "avg_loss": avg_l,
            "expectancy": ev,
            "jongbe_wr": _wr(jb),  "swing_wr":    _wr(sw),
            "jongbe_days": _days(jb), "swing_days": _days(sw),
        }

    def _calc_week_pnl(self) -> list[tuple]:
        if not self.db:
            return []
        trades  = self.db.get_trade_history(days=7)
        wd_map  = {0: "월", 1: "화", 2: "수", 3: "목", 4: "금"}
        acc: dict[str, list] = {}
        for t in trades:
            try:
                d  = datetime.datetime.strptime(str(t.get("date", ""))[:8], "%Y%m%d").date()
                wd = wd_map.get(d.weekday())
                if wd:
                    acc.setdefault(wd, []).append(float(t.get("pnl_pct") or 0))
            except Exception:
                pass
        return [(d, sum(ps) / len(ps)) for d, ps in acc.items()]

    def _show_performance_report(self) -> None:
        p   = self.calc_performance()
        msg = (
            f"=== 스윙 매매 성과 (최근 30일) ===\n\n"
            f"총 매매: {p.get('total', 0)}회\n"
            f"승: {p.get('win', 0)}  패: {p.get('lose', 0)}"
            f"  승률: {p.get('win_rate', 0):.1f}%\n"
            f"평균수익: {p.get('avg_win', 0):+.2f}%\n"
            f"평균손실: {p.get('avg_loss', 0):+.2f}%\n"
            f"기대값:  {p.get('expectancy', 0):+.3f}%\n\n"
            f"[전략별]\n"
            f"종베:  승률 {p.get('jongbe_wr', 0):.1f}%  /"
            f"  평균 {p.get('jongbe_days', 0):.1f}일\n"
            f"스윙:  승률 {p.get('swing_wr', 0):.1f}%  /"
            f"  평균 {p.get('swing_days', 0):.1f}일\n"
        )
        QMessageBox.information(self, "성과 리포트 (Ctrl+R)", msg)

    # ── 전체 갱신 ────────────────────────────────────────────

    def refresh_all(self) -> None:
        self.load_holdings()
        self.refresh_prices()

    # ── 설정 저장/복원 ──────────────────────────────────────

    def _restore_settings(self) -> None:
        s    = QSettings("StockCoding", "SwingDashboard")
        geo  = s.value("geometry")
        live = s.value("live_refresh", True, type=bool)
        if geo:
            self.restoreGeometry(geo)
        self._live = live
        self._live_btn.setChecked(live)
        if not live:
            self._price_timer.stop()

    def on_save_settings(self) -> None:
        s = QSettings("StockCoding", "SwingDashboard")
        s.setValue("geometry",     self.saveGeometry())
        s.setValue("live_refresh", self._live)

    def on_load_settings(self) -> None:
        geo = QSettings("StockCoding", "SwingDashboard").value("geometry")
        if geo:
            self.restoreGeometry(geo)

    def closeEvent(self, e) -> None:
        self.on_save_settings()
        if self._tray:
            self._tray.hide()
        super().closeEvent(e)


# ──────────────────────────────────────────────────────────────
# 단독 실행
# ──────────────────────────────────────────────────────────────

def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)
    dash = SwingDashboard()
    dash.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
