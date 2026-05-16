# -*- coding: utf-8 -*-
"""
전략 대시보드 (DashboardWidget)

4개 패널을 하나의 QWidget 탭으로 통합:
  1. 자금흐름 모니터 — LEVEL 1/2/3 업종 온도계 + 시간대 스케줄
  2. 종베 스캐너    — 수급 점수 체계 + 후보종목 C1~C6 테이블
  3. NXT 감시자    — 20:00 추가 외인 계산 + 보유/청산 판단
  4. 스윙 관리     — 보유종목 수익률/수급점수 + 자동 청산 트리거

사용:
    w = DashboardWidget()
    w.set_trader(trading_product_instance)
    mainTabWidget.addTab(w, "전략")
"""
from __future__ import annotations

import datetime
from typing import Optional

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QStackedWidget, QTableWidget, QTableWidgetItem, QHeaderView,
    QFrame, QScrollArea, QGroupBox, QGridLayout,
    QListWidget, QListWidgetItem, QAbstractItemView,
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QBrush


# ── 공통 헬퍼 ────────────────────────────────────────────────

def _cell(text: str, align: int = Qt.AlignCenter, bold: bool = False,
          fg: Optional[QColor] = None, bg: Optional[QColor] = None) -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    it.setTextAlignment(align)
    if bold:
        f = it.font(); f.setBold(True); it.setFont(f)
    if fg: it.setForeground(QBrush(fg))
    if bg: it.setBackground(QBrush(bg))
    return it


def _chip(text: str, fg: str = "#3949ab", bg: str = "#e8eaf6") -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"background:{bg}; color:{fg}; border-radius:10px;"
        "font-size:10px; padding:2px 8px;"
    )
    lbl.setFixedHeight(22)
    return lbl


def _level_hdr(num: str, title: str, tag: str) -> QWidget:
    w = QWidget()
    hl = QHBoxLayout(w)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.setSpacing(8)
    badge = QLabel(f" LEVEL {num} ")
    badge.setStyleSheet(
        "background:#e3f2fd; color:#1565c0; border-radius:3px;"
        "font-size:10px; font-weight:bold; padding:1px 5px;"
    )
    hl.addWidget(badge)
    lbl = QLabel(title)
    lbl.setStyleSheet("font-size:14px; font-weight:bold;")
    hl.addWidget(lbl)
    hl.addStretch()
    t = QLabel(tag)
    t.setStyleSheet("color:#888; font-size:11px;")
    hl.addWidget(t)
    return w


def _arrow() -> QLabel:
    a = QLabel("↓")
    a.setAlignment(Qt.AlignCenter)
    a.setStyleSheet("color:#bbb; font-size:16px;")
    a.setFixedHeight(22)
    return a


# ─────────────────────────────────────────────────────────────
# Panel 1: 자금흐름 모니터
# ─────────────────────────────────────────────────────────────

class FlowPanel(QWidget):
    _SCHED = [
        ("08:00",   True,  "NXT 프리마켓 — 전날 외인 대량매수 종목 갭 확인"),
        ("09~14시", False, "업종 온도 + 종목 수급 누적 추적"),
        ("14:30",   True,  "종베 스캐너 자동 실행 — 후보종목 팝업"),
        ("15:30",   False, "정규장 마감 수급 DB 저장"),
        ("20:00",   True,  "NXT 최종 수급 확정 — 보유/청산 자동판단"),
        ("20:30",   False, "내일 전략 리포트 자동 생성"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        # 전체를 스크롤 가능하게
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setStyleSheet("QScrollArea { border:none; }")
        inner = QWidget()
        vl = QVBoxLayout(inner)
        vl.setContentsMargins(16, 12, 16, 12)
        vl.setSpacing(10)

        # ── LEVEL 1 ──────────────────────────────────────────
        vl.addWidget(_level_hdr("1", "업종 온도계", "상시 실행"))

        hl_chips = QHBoxLayout()
        for t in ["외인 순매수 합계", "프로그램 순매수 합계", "거래대금 증가율", "섹터별 점수"]:
            hl_chips.addWidget(_chip(t))
        hl_chips.addStretch()
        vl.addLayout(hl_chips)

        # 섹터 카드 가로 스크롤
        card_sa = QScrollArea()
        card_sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        card_sa.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        card_sa.setFixedHeight(84)
        card_sa.setWidgetResizable(True)
        card_sa.setStyleSheet("QScrollArea { border:1px solid #eee; border-radius:4px; }")
        card_w = QWidget()
        self._card_hl = QHBoxLayout(card_w)
        self._card_hl.setSpacing(8)
        self._card_hl.setContentsMargins(8, 6, 8, 6)
        self._card_hl.addStretch()
        card_sa.setWidget(card_w)
        vl.addWidget(card_sa)

        vl.addWidget(_arrow())

        # ── LEVEL 2 ──────────────────────────────────────────
        vl.addWidget(_level_hdr("2", "종목별 수급 추적", "1분 갱신"))

        hl_chips2 = QHBoxLayout()
        for t in ["업종 내 대장주 자동선별", "외인 연속매수 일수", "프로그램 가속도"]:
            hl_chips2.addWidget(_chip(t))
        hl_chips2.addStretch()
        vl.addLayout(hl_chips2)

        self._lv2_lbl = QLabel("데이터 없음")
        self._lv2_lbl.setStyleSheet("color:#888; font-size:11px;")
        vl.addWidget(self._lv2_lbl)

        vl.addWidget(_arrow())

        # ── LEVEL 3 ──────────────────────────────────────────
        vl.addWidget(_level_hdr("3", "시간대 가중치 적용", "자동계산"))

        for time_str, is_star, desc in self._SCHED:
            row_w = QWidget()
            row_w.setFixedHeight(26)
            hl = QHBoxLayout(row_w)
            hl.setContentsMargins(4, 0, 4, 0)
            hl.setSpacing(8)

            t_lbl = QLabel(time_str)
            t_lbl.setFixedWidth(58)
            t_lbl.setStyleSheet(
                f"font-size:11px; font-weight:bold; "
                f"color:{'#e65100' if is_star else '#555'};"
            )
            hl.addWidget(t_lbl)

            icon_lbl = QLabel("★" if is_star else "●")
            icon_lbl.setFixedWidth(14)
            icon_lbl.setStyleSheet(
                f"color:{'#f57f17' if is_star else '#1976d2'}; font-size:9px;"
            )
            hl.addWidget(icon_lbl)

            d_lbl = QLabel(desc)
            d_lbl.setStyleSheet("color:#333; font-size:11px;")
            hl.addWidget(d_lbl)
            hl.addStretch()
            vl.addWidget(row_w)

        vl.addStretch()
        sa.setWidget(inner)
        outer.addWidget(sa)

    # ── 업데이트 ─────────────────────────────────────────────

    def update_sectors(self, sectors: dict) -> None:
        layout = self._card_hl
        while layout.count() > 1:
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        sorted_s = sorted(sectors.items(), key=lambda x: x[1].get("rank", 999))
        for name, data in sorted_s[:12]:
            temp = int(data.get("temperature", 50))
            layout.insertWidget(layout.count() - 1, self._make_card(name, temp))

    def _make_card(self, name: str, temp: int) -> QFrame:
        card = QFrame()
        card.setFixedSize(90, 64)
        if temp >= 70:   bg, tc = "#c8e6c9", "#1b5e20"
        elif temp >= 50: bg, tc = "#fff9c4", "#e65100"
        else:            bg, tc = "#ffccbc", "#c62828"
        card.setStyleSheet(
            f"QFrame {{ background:{bg}; border-radius:6px; border:1px solid #ddd; }}"
        )
        vl = QVBoxLayout(card)
        vl.setContentsMargins(4, 4, 4, 4)
        n = QLabel(name)
        n.setAlignment(Qt.AlignCenter)
        n.setStyleSheet("font-size:9px; font-weight:bold; color:#333;")
        n.setWordWrap(True)
        vl.addWidget(n)
        s = QLabel(f"{temp}점")
        s.setAlignment(Qt.AlignCenter)
        s.setStyleSheet(f"font-size:13px; font-weight:bold; color:{tc};")
        vl.addWidget(s)
        return card

    def update_leader(self, count: int) -> None:
        self._lv2_lbl.setText(
            f"대장주 {count}종목 수급 추적 중  |  {datetime.datetime.now():%H:%M:%S} 갱신"
        )


# ─────────────────────────────────────────────────────────────
# Panel 2: 종베 스캐너
# ─────────────────────────────────────────────────────────────

class BuySignalPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.scan_btn: Optional[QPushButton] = None
        self._build()

    def _build(self):
        vl = QVBoxLayout(self)
        vl.setContentsMargins(16, 12, 16, 12)
        vl.setSpacing(10)

        # 헤더
        hl0 = QHBoxLayout()
        title = QLabel("14:30 종가베팅 스캐너 — 조건 및 점수체계")
        title.setStyleSheet("font-size:12px; color:#555;")
        hl0.addWidget(title)
        hl0.addStretch()
        self.scan_btn = QPushButton("스캔 실행")
        self.scan_btn.setFixedSize(80, 26)
        self.scan_btn.setStyleSheet(
            "background:#1565c0; color:white; border-radius:3px; font-size:11px;"
        )
        hl0.addWidget(self.scan_btn)
        vl.addLayout(hl0)

        # 점수 카드 3개
        hl_cards = QHBoxLayout()
        hl_cards.setSpacing(8)
        hl_cards.addWidget(self._score_card(
            "외인 40점", "외인 수급",
            [("당일 순매수", "+20점"), ("3일 연속 매수", "+20점")],
            "#e3f2fd", "#1565c0"
        ))
        hl_cards.addWidget(self._score_card(
            "프로그램 35점", "프로그램 수급",
            [("순매수 양수", "+20점"), ("가속 중", "+15점")],
            "#e8f5e9", "#2e7d32"
        ))
        hl_cards.addWidget(self._score_card(
            "거래대금 25점", "거래량/업종",
            [("평균 2배 이상", "+15점"), ("업종 내 1위", "+10점")],
            "#fff3e0", "#e65100"
        ))
        vl.addLayout(hl_cards)

        # 진입 필터
        gb = QGroupBox("진입 필터  ·  눌림 조건")
        gb.setStyleSheet("QGroupBox { font-size:11px; font-weight:bold; }")
        gbl = QVBoxLayout(gb)
        chips_hl = QHBoxLayout()
        for label, ok in [
            ("고점 대비 3~7% 눌림  ✓", True),
            ("7% 초과 눌림  ✗", False),
            ("신고가 추격  ✗", False),
        ]:
            fg = "#2e7d32" if ok else "#c62828"
            bg = "#e8f5e9" if ok else "#ffebee"
            chips_hl.addWidget(_chip(label, fg, bg))
        chips_hl.addStretch()
        gbl.addLayout(chips_hl)
        note = QLabel("  70점 이상만 후보 등록 → 최대 5종목 → 점수순 정렬")
        note.setStyleSheet("color:#555; font-size:10px; margin-top:4px;")
        gbl.addWidget(note)
        vl.addWidget(gb)

        # 후보 결과 테이블
        self._result_lbl = QLabel("후보종목 (스캔 후 표시)")
        self._result_lbl.setStyleSheet("font-size:11px; font-weight:bold; color:#333;")
        vl.addWidget(self._result_lbl)

        cols = ["종목명", "섹터", "신호", "C1", "C2", "C3", "C4", "C5", "C6", "pass"]
        self._table = QTableWidget(0, len(cols))
        self._table.setHorizontalHeaderLabels(cols)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        for i in range(2, len(cols)):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.verticalHeader().setDefaultSectionSize(22)
        vl.addWidget(self._table)

    def _score_card(self, title: str, subtitle: str,
                    items: list, bg: str, fg: str) -> QFrame:
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background:{bg}; border-radius:8px; border:none; }}"
        )
        cvl = QVBoxLayout(card)
        cvl.setContentsMargins(10, 8, 10, 8)
        cvl.setSpacing(3)
        t = QLabel(title)
        t.setStyleSheet(f"font-size:12px; font-weight:bold; color:{fg};")
        cvl.addWidget(t)
        s = QLabel(subtitle)
        s.setStyleSheet("font-size:11px; font-weight:bold; color:#333;")
        cvl.addWidget(s)
        for label, pts in items:
            hl = QHBoxLayout()
            hl.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(label)
            lbl.setStyleSheet("font-size:10px; color:#555;")
            hl.addWidget(lbl)
            hl.addStretch()
            p = QLabel(pts)
            p.setStyleSheet(f"font-size:10px; font-weight:bold; color:{fg};")
            hl.addWidget(p)
            cvl.addLayout(hl)
        return card

    def update_signals(self, df) -> None:
        self._table.setRowCount(0)
        if df is None or (hasattr(df, "empty") and df.empty):
            self._result_lbl.setText("후보종목 (신호 없음)")
            return
        cnt = 0
        for _, row in df.iterrows():
            if cnt >= 5:
                break
            r = self._table.rowCount()
            self._table.insertRow(r)
            self._table.setItem(r, 0, _cell(str(row.get("종목명", "")),
                                             Qt.AlignLeft | Qt.AlignVCenter))
            self._table.setItem(r, 1, _cell(str(row.get("섹터", ""))))
            self._table.setItem(r, 2, _cell(str(row.get("signal", "")), bold=True))
            for ci, c in enumerate(["C1", "C2", "C3", "C4", "C5", "C6"], 3):
                val = bool(row.get(c, False))
                self._table.setItem(r, ci, _cell(
                    "✓" if val else "✗",
                    fg=QColor("#2e7d32") if val else QColor("#bbbbbb")
                ))
            self._table.setItem(r, 9, _cell(str(row.get("pass_cnt", ""))))
            cnt += 1
        self._result_lbl.setText(f"후보종목 ({len(df)}개 | 상위 {cnt}개 표시)")


# ─────────────────────────────────────────────────────────────
# Panel 3: NXT 감시자
# ─────────────────────────────────────────────────────────────

class NxtPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._vcnt: dict[str, QLabel] = {}
        self.run_btn: Optional[QPushButton] = None
        self._build()

    def _build(self):
        vl = QVBoxLayout(self)
        vl.setContentsMargins(16, 12, 16, 12)
        vl.setSpacing(10)

        # 헤더
        hl0 = QHBoxLayout()
        title = QLabel("NXT 20:00 수급 확정 — 보유/청산 자동판단")
        title.setStyleSheet("font-size:12px; color:#555;")
        hl0.addWidget(title)
        hl0.addStretch()
        self.run_btn = QPushButton("즉시 계산")
        self.run_btn.setFixedSize(80, 26)
        self.run_btn.setStyleSheet(
            "background:#1565c0; color:white; border-radius:3px; font-size:11px;"
        )
        hl0.addWidget(self.run_btn)
        vl.addLayout(hl0)

        # NXT 추가매수 계산 그룹
        calc_gb = QGroupBox("NXT 추가매수 계산")
        calc_gb.setStyleSheet(
            "QGroupBox { font-size:11px; font-weight:bold; }"
            "QGroupBox::title { color:#1565c0; }"
        )
        calc_vl = QVBoxLayout(calc_gb)
        formula = QLabel("NXT 추가매수  =  NXT 최종 외인  —  정규장 종료 외인")
        formula.setStyleSheet("font-size:11px; color:#555;")
        calc_vl.addWidget(formula)
        self._calc_list = QListWidget()
        self._calc_list.setFixedHeight(90)
        self._calc_list.setStyleSheet("font-size:11px; border:none;")
        calc_vl.addWidget(self._calc_list)
        vl.addWidget(calc_gb)

        # 판정 3열 카드
        verdict_hl = QHBoxLayout()
        verdict_hl.setSpacing(8)
        for key, title_v, bg, fg, bullets in [
            ("보유유지", "보유유지", "#e8f5e9", "#2e7d32",
             ["NXT 추가매수 양수", "내일 추가상승 기대"]),
            ("확인",    "확인",    "#fff8e1", "#f57f17",
             ["NXT 중립", "내일 오전 수급 체크"]),
            ("내일청산", "내일청산", "#ffebee", "#c62828",
             ["NXT 외인 30% 이상 이탈", "아침 동시호가 청산"]),
        ]:
            card = QFrame()
            card.setStyleSheet(
                f"QFrame {{ background:{bg}; border-radius:8px;"
                f"border:1px solid {fg}40; }}"
            )
            cvl = QVBoxLayout(card)
            cvl.setContentsMargins(10, 8, 10, 8)
            cvl.setSpacing(3)
            t = QLabel(title_v)
            t.setStyleSheet(f"font-size:12px; font-weight:bold; color:{fg};")
            cvl.addWidget(t)
            for b in bullets:
                bl = QLabel(b)
                bl.setStyleSheet("font-size:10px; color:#555;")
                cvl.addWidget(bl)
            cnt_lbl = QLabel("0종목")
            cnt_lbl.setStyleSheet(
                f"font-size:13px; font-weight:bold; color:{fg}; margin-top:4px;"
            )
            cvl.addWidget(cnt_lbl)
            self._vcnt[key] = cnt_lbl
            verdict_hl.addWidget(card)
        vl.addLayout(verdict_hl)

        # 자동 리포트 로그
        report_hdr = QLabel("20:30 자동 리포트")
        report_hdr.setStyleSheet("font-size:12px; font-weight:bold;")
        vl.addWidget(report_hdr)
        self._report_list = QListWidget()
        self._report_list.setStyleSheet("font-size:11px;")
        self._report_list.addItem("📦 NXT 마감 리포트 (20:00 이후 자동 생성)")
        vl.addWidget(self._report_list)

    def update_nxt(self, records: list[dict]) -> None:
        self._calc_list.clear()
        counts = {"보유유지": 0, "확인": 0, "내일청산": 0}

        for rec in records:
            nxt_gap  = float(rec.get("nxt_gap")     or 0)
            reg_fore = float(rec.get("regular_fore") or 1)
            name     = rec.get("name") or rec.get("code", "")
            threshold = abs(reg_fore) * 0.30

            if nxt_gap > 0:
                verdict, color = "보유유지", QColor("#2e7d32")
            elif nxt_gap < -threshold:
                verdict, color = "내일청산", QColor("#c62828")
            else:
                verdict, color = "확인",    QColor("#f57f17")
            counts[verdict] += 1

            item = QListWidgetItem(
                f"{name}: 정규 {reg_fore:+,.0f} / NXT 갭 {nxt_gap:+,.0f}백만 → {verdict}"
            )
            item.setForeground(QBrush(color))
            self._calc_list.addItem(item)

        if not records:
            self._calc_list.addItem("NXT 데이터 없음 (20:00 이후 갱신)")

        for key, lbl in self._vcnt.items():
            lbl.setText(f"{counts[key]}종목")

    def add_report(self, text: str) -> None:
        if (self._report_list.count() == 1 and
                "자동 생성" in (self._report_list.item(0).text() or "")):
            self._report_list.clear()
        self._report_list.addItem(text)
        if self._report_list.count() > 50:
            self._report_list.takeItem(0)


# ─────────────────────────────────────────────────────────────
# Panel 4: 스윙 관리
# ─────────────────────────────────────────────────────────────

class SwingPanel(QWidget):
    _EXITS = [
        ("익절", "#2e7d32", "●", "+5% 도달 시 절반 청산, 나머지는 스윙 유지"),
        ("손절", "#c62828", "●", "-3% 무조건 청산 (예외없음)"),
        ("수급", "#f57f17", "●", "NXT 외인 이탈 감지 → 다음날 아침 동시호가 청산"),
        ("기간", "#555555", "●", "최대 보유 5거래일 — 수급 무관 강제청산"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.refresh_cb = None   # DashboardWidget이 주입
        self._build()

    def _build(self):
        vl = QVBoxLayout(self)
        vl.setContentsMargins(16, 12, 16, 12)
        vl.setSpacing(10)

        # 헤더
        title_hl = QHBoxLayout()
        title_hl.addWidget(QLabel("스윙 관리 — 보유종목 점수 및 청산 기준"))
        title_hl.addStretch()
        refresh_btn = QPushButton("새로고침")
        refresh_btn.setFixedSize(72, 26)
        refresh_btn.setStyleSheet(
            "background:#546e7a; color:white; border-radius:3px; font-size:11px;"
        )
        refresh_btn.clicked.connect(lambda: self.refresh_cb and self.refresh_cb())
        title_hl.addWidget(refresh_btn)
        vl.addLayout(title_hl)

        # 보유종목 테이블
        cols = ["종목", "수익률(%)", "수급점수", "NXT", "액션"]
        self._table = QTableWidget(0, len(cols))
        self._table.setHorizontalHeaderLabels(cols)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, len(cols)):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setFixedHeight(160)
        self._table.verticalHeader().setDefaultSectionSize(24)
        vl.addWidget(self._table)

        # 청산 기준 자동 트리거
        gb = QGroupBox("청산 기준  ·  자동 트리거")
        gb.setStyleSheet("QGroupBox { font-size:11px; font-weight:bold; }")
        gb_vl = QVBoxLayout(gb)
        for label, fg, icon, desc in self._EXITS:
            row_w = QWidget()
            row_w.setFixedHeight(26)
            hl = QHBoxLayout(row_w)
            hl.setContentsMargins(4, 0, 4, 0)
            hl.setSpacing(8)
            lbl_w = QLabel(label)
            lbl_w.setFixedWidth(36)
            lbl_w.setStyleSheet(f"color:{fg}; font-size:11px; font-weight:bold;")
            hl.addWidget(lbl_w)
            icon_lbl = QLabel(icon)
            icon_lbl.setStyleSheet(f"color:{fg}; font-size:9px;")
            hl.addWidget(icon_lbl)
            desc_lbl = QLabel(desc)
            desc_lbl.setStyleSheet("color:#333; font-size:11px;")
            hl.addWidget(desc_lbl)
            hl.addStretch()
            gb_vl.addWidget(row_w)
        vl.addWidget(gb)
        vl.addStretch()

    def update_holdings(self, holdings: list[dict],
                        prices: dict, scores: dict) -> None:
        self._table.setRowCount(0)
        for h in holdings:
            code        = str(h.get("code", ""))
            name        = str(h.get("name", code))
            entry_price = float(h.get("entry_price") or 0)
            cur_price   = float(
                prices.get(code) or prices.get(code + "_AL") or 0
            )

            if entry_price and cur_price:
                pnl      = (cur_price - entry_price) / entry_price * 100.0
                pnl_str  = f"{pnl:+.1f}%"
                pnl_color = QColor(185, 30, 30) if pnl >= 0 else QColor(30, 30, 185)
            else:
                pnl_str, pnl_color = "--", None

            score    = int(scores.get(code, 0))
            nxt_stat = str(h.get("nxt_status") or "--")
            action   = str(h.get("status") or "보유")

            nxt_icon = {"보유": "✅", "확인": "⚠", "청산": "🔴"}.get(nxt_stat, nxt_stat)

            if score >= 70:   score_fg = QColor("#2e7d32")
            elif score >= 50: score_fg = QColor("#f57f17")
            else:             score_fg = QColor("#c62828")

            action_color = QColor("#2e7d32") if "보유" in action else QColor("#c62828")

            r = self._table.rowCount()
            self._table.insertRow(r)
            self._table.setItem(r, 0, _cell(name, Qt.AlignLeft | Qt.AlignVCenter, bold=True))
            self._table.setItem(r, 1, _cell(pnl_str, fg=pnl_color, bold=True))
            self._table.setItem(r, 2, _cell(f"{score}점", fg=score_fg))
            self._table.setItem(r, 3, _cell(nxt_icon))
            self._table.setItem(r, 4, _cell(action, fg=action_color, bold=True))


# ─────────────────────────────────────────────────────────────
# DashboardWidget — 4패널 통합 컨테이너
# ─────────────────────────────────────────────────────────────

_NAV_LABELS = ["자금흐름 모니터", "종베 스캐너", "NXT 감시자", "스윙 관리"]

_NAV_STYLE = """
QPushButton {
    background: white;
    border: 1px solid #ccc;
    border-radius: 4px;
    font-size: 12px;
    padding: 0 10px;
}
QPushButton:checked {
    background: #1565c0;
    color: white;
    border-color: #1565c0;
    font-weight: bold;
}
QPushButton:hover:!checked {
    background: #e3f2fd;
}
"""


class DashboardWidget(QWidget):
    """
    4개 전략 패널 통합 위젯.

    trading_product.py 에서:
        self._dashboard = DashboardWidget()
        self._dashboard.set_trader(self)
        self.mainTabWidget.addTab(self._dashboard, "전략")
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._trader = None
        self._build()

        # 1분 자동 갱신 (현재 보이는 패널만)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(60_000)

    def _build(self):
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        # ── 네비 바 ──────────────────────────────────────────
        nav = QWidget()
        nav.setFixedHeight(44)
        nav.setStyleSheet("background:#f5f5f5; border-bottom:1px solid #ddd;")
        hl = QHBoxLayout(nav)
        hl.setContentsMargins(10, 6, 10, 6)
        hl.setSpacing(6)

        self._nav_btns: list[QPushButton] = []
        for i, label in enumerate(_NAV_LABELS):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(30)
            btn.setMinimumWidth(110)
            btn.setStyleSheet(_NAV_STYLE)
            btn.clicked.connect(lambda _, idx=i: self._switch(idx))
            self._nav_btns.append(btn)
            hl.addWidget(btn)
        hl.addStretch()
        vl.addWidget(nav)

        # ── 스택 ─────────────────────────────────────────────
        self._stack = QStackedWidget()
        self._flow_panel  = FlowPanel()
        self._buy_panel   = BuySignalPanel()
        self._nxt_panel   = NxtPanel()
        self._swing_panel = SwingPanel()

        # 버튼 → 콜백 연결
        self._buy_panel.scan_btn.clicked.connect(self._refresh_buy_signal)
        self._nxt_panel.run_btn.clicked.connect(self._refresh_nxt)
        self._swing_panel.refresh_cb = self._refresh_swing

        for panel in (self._flow_panel, self._buy_panel,
                      self._nxt_panel, self._swing_panel):
            self._stack.addWidget(panel)
        vl.addWidget(self._stack)

        self._switch(0)

    def _switch(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)
        for i, btn in enumerate(self._nav_btns):
            btn.setChecked(i == idx)
        self.refresh()

    # ── 외부 인터페이스 ──────────────────────────────────────

    def set_trader(self, trader) -> None:
        self._trader = trader
        self.refresh()

    def refresh(self) -> None:
        """현재 보이는 패널만 갱신."""
        idx = self._stack.currentIndex()
        if idx == 0: self._refresh_flow()
        elif idx == 1: self._refresh_buy_signal()
        elif idx == 2: self._refresh_nxt()
        elif idx == 3: self._refresh_swing()

    def on_sector_update(self) -> None:
        """_on_sector_summary_updated 콜백에서 호출 — 자금흐름 즉시 반영."""
        if self._stack.currentIndex() == 0:
            self._refresh_flow()

    def on_buy_signal(self, df) -> None:
        """_on_buy_signal_updated 콜백에서 호출 — 종베 스캐너 즉시 반영."""
        self._buy_panel.update_signals(df)

    # ── 패널별 갱신 ──────────────────────────────────────────

    def _refresh_flow(self) -> None:
        if self._trader is None:
            return
        try:
            from flow_monitor import calc_temperature
            summary = self._trader.sector_analyzer.get_summary()
            sectors: dict = {}
            for rank, (_, row) in enumerate(summary.iterrows(), 1):
                name  = str(row.get("섹터명", ""))
                fore  = float(row.get("외인순매수합", 0) or 0) / 100.0
                prog  = float(row.get("프로그램합",   0) or 0) / 100.0
                vol   = float(row.get("거래대금증가율", 0) or 0)
                sectors[name] = {
                    "rank":        rank,
                    "temperature": calc_temperature(fore, prog, vol),
                }
            self._flow_panel.update_sectors(sectors)
        except Exception:
            pass
        try:
            leader_df = self._trader.sector_analyzer.get_leader_scores()
            if leader_df is not None and not leader_df.empty:
                self._flow_panel.update_leader(len(leader_df))
        except Exception:
            pass

    def _refresh_buy_signal(self) -> None:
        """스캔 실행 버튼 — ScanPopup 열기 + 테이블 동시 갱신."""
        # 테이블 갱신 (C1~C6 요약)
        if self._trader is not None:
            try:
                df = self._trader.buy_signal_scanner.get_signals()
                self._buy_panel.update_signals(df)
            except Exception:
                pass

        # ScanPopup 열기 (진입가/목표가/손절가 카드)
        try:
            from scan_popup import auto_popup_at_1430
            auto_popup_at_1430(trader=self._trader, parent=self)
        except Exception as e:
            from PyQt5.QtWidgets import QMessageBox
            QMessageBox.warning(self, "스캐너 오류", f"스캔 실행 실패: {e}")

    def _refresh_nxt(self) -> None:
        if self._trader is None:
            return
        try:
            records = self._trader.db.get_nxt_yesterday()
            self._nxt_panel.update_nxt(records)
        except Exception:
            pass

    def _refresh_swing(self) -> None:
        if self._trader is None:
            return
        try:
            holdings = self._trader.db.get_holding_list()
            prices   = self._trader.stock_code_to_realtime_price_dict

            scores: dict[str, int] = {}
            try:
                leader_df = self._trader.sector_analyzer.get_leader_scores()
                if leader_df is not None and not leader_df.empty:
                    for _, r in leader_df.iterrows():
                        code  = str(r.get("종목코드", ""))
                        score = int(float(r.get("leader_score", 0) or 0))
                        scores[code] = score
            except Exception:
                pass

            self._swing_panel.update_holdings(holdings, prices, scores)
        except Exception:
            pass
