# -*- coding: utf-8 -*-
"""
NXT 감시자 (NXTWatcher)

장후 NXT 추가 외인 매수를 추적해 보유종목 판정.

시간 트리거:
  15:30             → save_regular_close()  정규장 종료 외인 캐시
  18:00~19:55 매 5분 → check_holding_nxt()  중간 점검
  20:00             → collect_final_nxt()   최종 확정 + DB 저장
  20:30             → generate_report()     내일 전략 리포트 생성

판정 기준:
  nxt_gap >  30% × |정규외인|  → ✅ 강력보유
  nxt_gap >  0                  → ✅ 보유유지
  nxt_gap in [-30%, -15%]      → ⚠️ 재확인
  nxt_gap < -30% × |정규외인|  → 🚨 내일청산

단독 실행:
    python nxt_watcher.py

외부 호출 (20:30 자동 팝업):
    from nxt_watcher import auto_popup_at_2030
    auto_popup_at_2030(trader=self, parent=self)
"""
from __future__ import annotations

import datetime
import os
import sys
import time
from typing import Optional

try:
    from loguru import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from PyQt5.QtWidgets import (
    QApplication, QDialog, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QFrame, QListWidget, QListWidgetItem,
    QMessageBox, QAbstractItemView, QTextEdit,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QSettings
from PyQt5.QtGui import QColor, QBrush

_CHAPTER_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_CHAPTER_DIR)
_REPORT_DIR  = os.path.join(_PROJECT_DIR, "data", "reports")

if _CHAPTER_DIR not in sys.path:
    sys.path.insert(0, _CHAPTER_DIR)

try:
    from db_manager import DBManager, DB_PATH
    _DB_OK = True
except ImportError:
    _DB_OK = False


# ──────────────────────────────────────────────────────────────
# 헬퍼
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


def judge_verdict(regular_fore: float, nxt_fore: float, nxt_gap: float) -> str:
    """
    정규 외인 대비 NXT 갭으로 보유/청산 판정.

    반환: '강력보유' | '보유유지' | '재확인' | '내일청산'
    """
    base  = abs(regular_fore) if regular_fore != 0 else 1.0
    ratio = nxt_gap / base

    if nxt_gap > base * 0.30:
        return "강력보유"
    if nxt_gap > 0:
        return "보유유지"
    if ratio < -0.30:
        return "내일청산"
    if ratio < -0.15:
        return "재확인"
    return "보유유지"


_VERDICT_ICON = {
    "강력보유": "✅",
    "보유유지": "✅",
    "재확인":   "⚠️",
    "내일청산": "🚨",
}

_VERDICT_COLOR = {
    "강력보유": "#1b5e20",
    "보유유지": "#2e7d32",
    "재확인":   "#f57f17",
    "내일청산": "#c62828",
}


# ──────────────────────────────────────────────────────────────
# NXTCollector — 백그라운드 수집 스레드
# ──────────────────────────────────────────────────────────────

class NXTCollector(QThread):
    """
    장후 NXT 데이터 수집 스레드.

    Kiwoom COM API는 메인 스레드에서만 호출 가능하므로
    이 스레드는 DB / trader 캐시(dict, DataFrame)만 읽음.
    UI 업데이트는 pyqtSignal 만 사용.
    """

    data_ready      = pyqtSignal(list)      # 개별 종목 데이터 갱신
    alert           = pyqtSignal(str, str)  # (종목명, 메시지)
    collection_done = pyqtSignal(list)      # 배치 완료 (레코드 리스트)
    report_ready    = pyqtSignal(str, list) # (리포트 텍스트, 레코드 리스트)
    status_changed  = pyqtSignal(str)       # 상태 텍스트

    _MAX_RETRY = 3
    _RETRY_SEC = 0.2

    def __init__(self, trader=None, db=None, parent=None) -> None:
        super().__init__(parent)
        self._trader          = trader
        self._db              = db
        self._stop_flag       = False
        self._today           = datetime.date.today().strftime("%Y%m%d")
        self._regular_snap:   dict[str, float] = {}
        self._records:        list[dict]       = []

    def stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        self.status_changed.emit("NXT 수집 시작...")
        done_steps: set[str] = set()

        # 시작 시점이 15:30 이후면 정규 종료 데이터 즉시 읽기
        now = datetime.datetime.now()
        if now.hour > 15 or (now.hour == 15 and now.minute >= 30):
            try:
                self._do_save_regular()
            except Exception as e:
                self.alert.emit("시스템", f"정규 초기화 실패: {e}")
            done_steps.add("15:30")

        while not self._stop_flag:
            now   = datetime.datetime.now()
            t_key = now.strftime("%H:%M")

            if t_key not in done_steps:
                action = self._get_action(t_key)
                if action:
                    done_steps.add(t_key)
                    self.status_changed.emit(f"{t_key} 실행 중...")
                    for attempt in range(self._MAX_RETRY):
                        try:
                            action()
                            break
                        except Exception as e:
                            if attempt == self._MAX_RETRY - 1:
                                self.alert.emit("시스템", f"{t_key} 실패: {e}")
                            else:
                                time.sleep(self._RETRY_SEC)
                    if t_key == "20:30":
                        break

            self.msleep(30_000)  # 30초 간격으로 시각 재확인

        self.status_changed.emit("NXT 수집 완료")

    def _get_action(self, t_key: str):
        """시각 키 → 실행 메서드 반환. 해당 없으면 None."""
        if t_key == "15:30":
            return self._do_save_regular
        if t_key == "20:00":
            return self._do_collect_final
        if t_key == "20:30":
            return self._do_generate_report
        try:
            h, m = int(t_key[:2]), int(t_key[3:])
            if 18 <= h <= 19 and m % 5 == 0:
                return self._do_check_nxt
        except ValueError:
            pass
        return None

    # ── 실제 작업 메서드 ─────────────────────────────────────

    def _do_save_regular(self) -> None:
        """15:30 정규장 종료 외인 캐시 저장."""
        today = self._today
        snap: dict[str, float] = {}

        if self._db and hasattr(self._db, "_conn"):
            cur = self._db._conn.cursor()
            cur.execute("""
                SELECT code, foreigner_buy
                FROM supply_flow
                WHERE date = ? AND time <= '1540' AND source = 'regular'
                ORDER BY time DESC
            """, (today,))
            seen: set[str] = set()
            for code, fore in cur.fetchall():
                if code not in seen:
                    seen.add(code)
                    snap[code] = float(fore or 0)

        if not snap and self._trader is not None:
            try:
                df = self._trader.sector_analyzer.get_leader_scores()
                if df is not None and not df.empty:
                    for _, r in df.iterrows():
                        code = str(r.get("종목코드", ""))
                        fore = float(r.get("외인순매수", 0) or 0)
                        if code:
                            snap[code] = fore
            except Exception:
                pass

        self._regular_snap = snap
        self.status_changed.emit(f"정규 종료 외인 {len(snap)}종목 캐시 완료")

    def _do_check_nxt(self) -> None:
        """18:00~19:55 매 5분 — 보유종목 NXT 현황 점검 + 이탈 경보."""
        records = self._build_records()
        self.data_ready.emit(records)

        for rec in records:
            v    = rec.get("verdict", "")
            name = rec.get("name") or rec.get("code", "")
            gap  = float(rec.get("nxt_gap") or 0)
            if v == "내일청산":
                self.alert.emit(name, f"🚨 NXT 이탈 감지: {gap/100:+.0f}억")
            elif v == "재확인":
                self.alert.emit(name, f"⚠️ NXT 재확인 필요: {gap/100:+.0f}억")

    def _do_collect_final(self) -> None:
        """20:00 최종 NXT 수급 확정 + DB 저장."""
        records = self._build_records()
        self._records = records

        if self._db:
            for rec in records:
                try:
                    self._db.save_nxt(rec)
                except Exception as e:
                    logger.warning(f"[NXT] save_nxt 실패 ({rec.get('code')}): {e}")

        self.collection_done.emit(records)
        self.status_changed.emit(f"NXT 최종 확정: {len(records)}종목")

    def _do_generate_report(self) -> None:
        """20:30 내일 전략 리포트 생성 + 파일 저장."""
        if not self._records:
            self._records = self._build_records()
        today   = self._today
        now_str = datetime.datetime.now().strftime("%H:%M")
        text    = self._format_report(self._records, today, now_str)

        os.makedirs(_REPORT_DIR, exist_ok=True)
        fpath = os.path.join(_REPORT_DIR, f"{today}_nxt.txt")
        try:
            with open(fpath, "w", encoding="utf-8") as fp:
                fp.write(text)
            logger.info(f"[NXT] 리포트 저장: {fpath}")
        except Exception as e:
            logger.warning(f"[NXT] 리포트 파일 저장 실패: {e}")

        self.report_ready.emit(text, self._records)

    # ── 데이터 조합 ──────────────────────────────────────────

    def _build_records(self) -> list[dict]:
        """보유종목 × 정규/NXT 외인 → 판정 레코드 리스트."""
        holdings = self._get_holdings()
        records: list[dict] = []

        for h in holdings:
            code         = str(h.get("code", ""))
            name         = str(h.get("name") or code)
            regular_fore = float(self._regular_snap.get(code) or
                                 h.get("regular_fore") or 0)
            nxt_fore     = self._get_nxt_fore(code)
            nxt_gap      = nxt_fore - regular_fore
            verdict      = judge_verdict(regular_fore, nxt_fore, nxt_gap)

            records.append({
                "date":         self._today,
                "code":         code,
                "name":         name,
                "regular_fore": regular_fore,
                "nxt_fore":     nxt_fore,
                "nxt_gap":      nxt_gap,
                "verdict":      verdict,
                "next_open":    None,
                "next_pct":     None,
            })

        return records

    def _get_holdings(self) -> list[dict]:
        """보유종목 리스트 조회 (DB → trader 순)."""
        if self._db and hasattr(self._db, "_conn"):
            try:
                cur = self._db._conn.cursor()
                cur.execute("SELECT code, name FROM holding WHERE status = '보유'")
                return [{"code": r[0], "name": r[1]} for r in cur.fetchall()]
            except Exception:
                pass
        if self._trader is not None:
            try:
                return self._trader.db.get_holding_list()
            except Exception:
                pass
        return []

    def _get_nxt_fore(self, code: str) -> float:
        """NXT 장후 외인 최신값 조회 (DB → trader 캐시 순)."""
        today = self._today
        if self._db and hasattr(self._db, "_conn"):
            try:
                cur = self._db._conn.cursor()
                cur.execute("""
                    SELECT foreigner_buy FROM supply_flow
                    WHERE date = ? AND code = ? AND source = 'nxt'
                    ORDER BY time DESC LIMIT 1
                """, (today, code))
                row = cur.fetchone()
                if row:
                    return float(row[0] or 0)
            except Exception:
                pass
        if self._trader is not None:
            try:
                df = self._trader.sector_analyzer.get_leader_scores()
                if df is not None and not df.empty:
                    mask = df["종목코드"] == code
                    if mask.any():
                        return float(df.loc[mask, "외인순매수"].iloc[0] or 0)
            except Exception:
                pass
        return 0.0

    # ── 리포트 포매터 ─────────────────────────────────────────

    @staticmethod
    def _format_report(records: list[dict], today: str, now_str: str) -> str:
        date_fmt = f"{today[:4]}-{today[4:6]}-{today[6:]}"
        lines = [
            "=== NXT 마감 리포트 ===",
            f"날짜: {date_fmt}",
            f"생성: {now_str}",
            "",
            "[보유판정]",
        ]

        groups: dict[str, list] = {
            "강력보유": [], "보유유지": [], "재확인": [], "내일청산": []
        }
        for rec in records:
            v = rec.get("verdict", "보유유지")
            groups.get(v, groups["보유유지"]).append(rec)

        for v in ("강력보유", "보유유지", "재확인", "내일청산"):
            icon = _VERDICT_ICON.get(v, "")
            for rec in groups[v]:
                name = rec.get("name") or rec.get("code", "")
                gap  = float(rec.get("nxt_gap") or 0)
                base = abs(float(rec.get("regular_fore") or 1))
                pct  = gap / base * 100 if base else 0
                gap_str = f"{gap/100:+.0f}억"
                pct_str = f", {pct:+.0f}%" if v in ("재확인", "내일청산") else ""
                lines.append(f"{icon} {v}: {name} ({gap_str}{pct_str})")

        # 내일 전략 요약
        lines += ["", "[내일전략]"]
        hold = [rec.get("name") or rec.get("code", "")
                for rec in groups["강력보유"] + groups["보유유지"]]
        chk  = [rec.get("name") or rec.get("code", "")
                for rec in groups["재확인"]]
        sell = [rec.get("name") or rec.get("code", "")
                for rec in groups["내일청산"]]
        if hold:
            lines.append(f"보유: {', '.join(hold)}")
        if chk:
            lines.append(f"확인: {', '.join(chk)}")
        if sell:
            lines.append(f"청산: {', '.join(sell)}")

        # NXT 급증 신규 후보 (placeholder — 별도 스캔 필요)
        lines += ["", "[NXT급증 신규후보]"]
        lines.append("(데이터 없음 — 별도 스캔 필요)")

        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# NXTReportPopup — 20:30 리포트 다이얼로그
# ──────────────────────────────────────────────────────────────

class NXTReportPopup(QDialog):
    def __init__(self, report_text: str, records: list[dict],
                 trader=None, parent=None) -> None:
        super().__init__(parent)
        self._records = records
        self._trader  = trader
        self.setWindowTitle("NXT 마감 리포트")
        self.setMinimumSize(580, 540)
        s = QSettings("StockCoding", "NXTReportPopup")
        geo = s.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        self._build(report_text, records)

    def closeEvent(self, e) -> None:
        QSettings("StockCoding", "NXTReportPopup").setValue(
            "geometry", self.saveGeometry()
        )
        super().closeEvent(e)

    def _build(self, report_text: str, records: list[dict]) -> None:
        vl = QVBoxLayout(self)
        vl.setContentsMargins(16, 12, 16, 12)
        vl.setSpacing(10)

        hdr = QLabel("NXT 마감 리포트  —  20:30 자동 생성")
        hdr.setStyleSheet("font-size:13px; font-weight:bold; color:#1565c0;")
        vl.addWidget(hdr)

        # 판정 4구역 카드
        groups: dict[str, list] = {
            "강력보유": [], "보유유지": [], "재확인": [], "내일청산": []
        }
        for rec in records:
            v = rec.get("verdict", "보유유지")
            groups.get(v, groups["보유유지"]).append(rec)

        cards_hl = QHBoxLayout()
        cards_hl.setSpacing(8)
        for v, bg, fg in [
            ("강력보유", "#e8f5e9", "#1b5e20"),
            ("보유유지", "#f1f8e9", "#2e7d32"),
            ("재확인",   "#fff8e1", "#f57f17"),
            ("내일청산", "#ffebee", "#c62828"),
        ]:
            cards_hl.addWidget(self._verdict_card(v, groups[v], bg, fg))
        vl.addLayout(cards_hl)

        # 리포트 전문
        text_lbl = QLabel("전체 리포트")
        text_lbl.setStyleSheet("font-size:12px; font-weight:bold;")
        vl.addWidget(text_lbl)

        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setPlainText(report_text)
        self._text_edit.setStyleSheet(
            "font-family: Consolas, monospace; font-size:11px;"
        )
        self._text_edit.setFixedHeight(190)
        vl.addWidget(self._text_edit)

        # 버튼 바
        btn_hl = QHBoxLayout()
        btn_hl.addStretch()
        save_btn = QPushButton("파일 저장")
        save_btn.setFixedSize(90, 28)
        save_btn.setStyleSheet(
            "background:#546e7a; color:white; border-radius:3px; font-size:11px;"
        )
        save_btn.clicked.connect(lambda: self._save_report(report_text))
        btn_hl.addWidget(save_btn)
        close_btn = QPushButton("닫기")
        close_btn.setFixedSize(72, 28)
        close_btn.setStyleSheet(
            "background:#e0e0e0; color:#333; border-radius:3px; font-size:11px;"
        )
        close_btn.clicked.connect(self.accept)
        btn_hl.addWidget(close_btn)
        vl.addLayout(btn_hl)

    def _verdict_card(self, verdict: str, items: list[dict],
                      bg: str, fg: str) -> QFrame:
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background:{bg}; border-radius:8px;"
            f"border:1px solid {fg}40; }}"
        )
        cvl = QVBoxLayout(card)
        cvl.setContentsMargins(8, 6, 8, 8)
        cvl.setSpacing(3)
        icon = _VERDICT_ICON.get(verdict, "")
        hdr_lbl = QLabel(f"{icon} {verdict}")
        hdr_lbl.setStyleSheet(
            f"font-size:12px; font-weight:bold; color:{fg};"
        )
        cvl.addWidget(hdr_lbl)
        cnt_lbl = QLabel(f"{len(items)}종목")
        cnt_lbl.setStyleSheet(
            f"font-size:18px; font-weight:bold; color:{fg};"
        )
        cvl.addWidget(cnt_lbl)
        for rec in items[:4]:
            name = rec.get("name") or rec.get("code", "")
            gap  = float(rec.get("nxt_gap") or 0)
            lbl  = QLabel(f"{name}  {gap/100:+.0f}억")
            lbl.setStyleSheet("font-size:10px; color:#555;")
            cvl.addWidget(lbl)
        if len(items) > 4:
            more = QLabel(f"외 {len(items) - 4}종목")
            more.setStyleSheet("font-size:10px; color:#888;")
            cvl.addWidget(more)
        cvl.addStretch()
        return card

    def _save_report(self, text: str) -> None:
        os.makedirs(_REPORT_DIR, exist_ok=True)
        today = datetime.date.today().strftime("%Y%m%d")
        fpath = os.path.join(_REPORT_DIR, f"{today}_nxt.txt")
        try:
            with open(fpath, "w", encoding="utf-8") as fp:
                fp.write(text)
            QMessageBox.information(self, "저장 완료", f"리포트 저장:\n{fpath}")
        except Exception as e:
            QMessageBox.warning(self, "저장 실패", str(e))


# ──────────────────────────────────────────────────────────────
# NXTWatcher — 메인 모니터링 위젯
# ──────────────────────────────────────────────────────────────

class NXTWatcher(QWidget):
    """
    NXT 감시자 메인 위젯.

    단독 창으로 열거나 DashboardWidget 에 임베드 가능.
    """

    _COLS = ["종목명", "정규외인", "NXT외인", "NXT갭", "판정", "내일시가", "익일등락"]

    def __init__(self, trader=None, db=None, parent=None) -> None:
        super().__init__(parent)
        self._trader    = trader
        self._db:       Optional[DBManager] = db
        self._collector: Optional[NXTCollector] = None
        self._records:  list[dict] = []
        self._build()
        self._start_collector()

        self._countdown_timer = QTimer(self)
        self._countdown_timer.timeout.connect(self._update_countdown)
        self._countdown_timer.start(5_000)
        self._update_countdown()

    # ── UI 구성 ──────────────────────────────────────────────

    def _build(self) -> None:
        vl = QVBoxLayout(self)
        vl.setContentsMargins(12, 10, 12, 10)
        vl.setSpacing(8)

        # 헤더 바
        hl_hdr = QHBoxLayout()
        self._status_lbl = QLabel("NXT 감시자 대기 중...")
        self._status_lbl.setStyleSheet("font-size:11px; color:#555;")
        hl_hdr.addWidget(self._status_lbl)
        hl_hdr.addStretch()

        self._countdown_lbl = QLabel("")
        self._countdown_lbl.setStyleSheet("font-size:11px; color:#1565c0;")
        hl_hdr.addWidget(self._countdown_lbl)

        collect_btn = QPushButton("지금수집")
        collect_btn.setFixedSize(72, 26)
        collect_btn.setStyleSheet(
            "background:#1565c0; color:white; border-radius:3px; font-size:11px;"
        )
        collect_btn.clicked.connect(self._on_collect_now)
        hl_hdr.addWidget(collect_btn)

        report_btn = QPushButton("리포트생성")
        report_btn.setFixedSize(80, 26)
        report_btn.setStyleSheet(
            "background:#2e7d32; color:white; border-radius:3px; font-size:11px;"
        )
        report_btn.clicked.connect(self._on_make_report)
        hl_hdr.addWidget(report_btn)
        vl.addLayout(hl_hdr)

        # 7컬럼 테이블
        self._table = QTableWidget(0, len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, len(self._COLS)):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.verticalHeader().setDefaultSectionSize(24)
        self._table.setFixedHeight(200)
        vl.addWidget(self._table)

        # 알림 로그
        alert_hdr = QLabel("알림 로그")
        alert_hdr.setStyleSheet("font-size:12px; font-weight:bold;")
        vl.addWidget(alert_hdr)

        self._alert_list = QListWidget()
        self._alert_list.setStyleSheet("font-size:11px;")
        self._alert_list.addItem("📦 NXT 수집 대기 중 (18:00 자동 시작)")
        vl.addWidget(self._alert_list)

    # ── 수집기 연결 ──────────────────────────────────────────

    def _start_collector(self) -> None:
        if self._collector and self._collector.isRunning():
            return
        db = self._db or (self._trader.db if self._trader else None)
        self._collector = NXTCollector(trader=self._trader, db=db)
        self._collector.data_ready.connect(self._on_data_ready)
        self._collector.alert.connect(self._on_alert)
        self._collector.collection_done.connect(self._on_collection_done)
        self._collector.report_ready.connect(self._on_report_ready)
        self._collector.status_changed.connect(self._on_status)
        self._collector.start()

    def _stop_collector(self) -> None:
        if self._collector:
            self._collector.stop()
            self._collector.wait(2000)

    def closeEvent(self, e) -> None:
        self._stop_collector()
        super().closeEvent(e)

    # ── 슬롯 ─────────────────────────────────────────────────

    def _on_data_ready(self, records: list[dict]) -> None:
        self._records = records
        self._refresh_table(records)

    def _on_alert(self, name: str, msg: str) -> None:
        ts   = datetime.datetime.now().strftime("%H:%M")
        item = QListWidgetItem(f"[{ts}] {name} — {msg}")
        if "🚨" in msg:
            item.setForeground(QBrush(QColor("#c62828")))
        elif "⚠️" in msg:
            item.setForeground(QBrush(QColor("#f57f17")))
        self._alert_list.insertItem(0, item)
        if self._alert_list.count() > 100:
            self._alert_list.takeItem(self._alert_list.count() - 1)

    def _on_collection_done(self, records: list[dict]) -> None:
        self._records = records
        self._refresh_table(records)
        self._on_alert("시스템", f"✅ 수집 완료: {len(records)}종목")

    def _on_report_ready(self, text: str, records: list[dict]) -> None:
        self._records = records
        self._refresh_table(records)
        popup = NXTReportPopup(text, records, trader=self._trader, parent=self)
        popup.exec_()

    def _on_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)

    def _on_collect_now(self) -> None:
        """지금수집 버튼 — 즉시 NXT 데이터 읽기 (스레드 없이 직접 실행)."""
        db = self._db or (self._trader.db if self._trader else None)
        coll = NXTCollector(trader=self._trader, db=db)
        self._on_status("즉시 수집 중...")
        try:
            coll._do_save_regular()
            records = coll._build_records()
            self._on_data_ready(records)
            self._on_status(f"즉시 수집 완료 ({len(records)}종목)")
        except Exception as e:
            self._on_alert("시스템", f"즉시 수집 실패: {e}")
            self._on_status("즉시 수집 실패")

    def _on_make_report(self) -> None:
        """리포트생성 버튼 — 현재 records 기준 리포트 팝업."""
        if not self._records:
            QMessageBox.information(
                self, "리포트", "수집된 데이터가 없습니다.\n'지금수집' 후 시도하세요."
            )
            return
        today   = datetime.date.today().strftime("%Y%m%d")
        now_str = datetime.datetime.now().strftime("%H:%M")
        text    = NXTCollector._format_report(self._records, today, now_str)
        popup   = NXTReportPopup(text, self._records, trader=self._trader, parent=self)
        popup.exec_()

    # ── 테이블 갱신 ───────────────────────────────────────────

    def _refresh_table(self, records: list[dict]) -> None:
        self._table.setRowCount(0)
        for rec in records:
            r        = self._table.rowCount()
            self._table.insertRow(r)
            name     = rec.get("name") or rec.get("code", "")
            reg_fore = float(rec.get("regular_fore") or 0)
            nxt_fore = float(rec.get("nxt_fore")     or 0)
            nxt_gap  = float(rec.get("nxt_gap")      or 0)
            verdict  = str(rec.get("verdict") or "보유유지")
            next_open = rec.get("next_open")
            next_pct  = rec.get("next_pct")
            fg        = QColor(_VERDICT_COLOR.get(verdict, "#333333"))

            self._table.setItem(r, 0, _cell(
                name, Qt.AlignLeft | Qt.AlignVCenter, bold=True
            ))
            self._table.setItem(r, 1, _cell(
                f"{reg_fore/100:+.0f}억" if reg_fore else "--"
            ))
            self._table.setItem(r, 2, _cell(
                f"{nxt_fore/100:+.0f}억" if nxt_fore else "--"
            ))
            self._table.setItem(r, 3, _cell(
                f"{nxt_gap/100:+.0f}억", fg=fg, bold=True
            ))
            icon = _VERDICT_ICON.get(verdict, "")
            self._table.setItem(r, 4, _cell(
                f"{icon} {verdict}", fg=fg, bold=True
            ))
            self._table.setItem(r, 5, _cell(
                f"{next_open:,.0f}" if next_open else "--"
            ))
            if next_pct is not None:
                pct_fg = QColor(185, 30, 30) if next_pct >= 0 else QColor(30, 30, 185)
                self._table.setItem(r, 6, _cell(f"{next_pct:+.2f}%", fg=pct_fg))
            else:
                self._table.setItem(r, 6, _cell("--"))

    # ── 카운트다운 ────────────────────────────────────────────

    def _update_countdown(self) -> None:
        now = datetime.datetime.now()
        for t in (datetime.time(15, 30), datetime.time(20, 0), datetime.time(20, 30)):
            target = datetime.datetime.combine(now.date(), t)
            if target > now:
                delta = target - now
                mins  = int(delta.total_seconds() // 60)
                secs  = int(delta.total_seconds() % 60)
                self._countdown_lbl.setText(
                    f"다음 이벤트({t.strftime('%H:%M')})까지 {mins:02d}:{secs:02d}"
                )
                return
        self._countdown_lbl.setText("")


# ──────────────────────────────────────────────────────────────
# update_next_day_result — 09:10 익일 결과 업데이트
# ──────────────────────────────────────────────────────────────

def update_next_day_result(db: "DBManager", open_prices: Optional[dict] = None) -> None:
    """
    09:10 호출 — 어제 nxt_data의 next_open / next_pct 업데이트.

    open_prices: {code: open_price} dict (trader 로부터 주입)
    trading_product.py _update_nxt_next_day() 에서 호출.
    """
    yesterday = (
        datetime.date.today() - datetime.timedelta(days=1)
    ).strftime("%Y%m%d")
    if not open_prices:
        return
    try:
        cur = db._conn.cursor()
        cur.execute(
            "SELECT id, code, regular_fore FROM nxt_data WHERE date = ? AND next_open IS NULL",
            (yesterday,)
        )
        rows = cur.fetchall()
        if not rows:
            return
        updated = 0
        for row_id, code, regular_fore in rows:
            open_p = open_prices.get(code)
            if open_p is None:
                continue
            entry_p = float(regular_fore or open_p or 0)
            pct     = (open_p - entry_p) / entry_p * 100.0 if entry_p else 0.0
            db.update_nxt_next_day(yesterday, code, open_p, pct)
            updated += 1
        logger.info(f"[NXT] {yesterday} 익일 결과 업데이트: {updated}/{len(rows)}건")
    except Exception as e:
        logger.warning(f"[NXT] 익일 결과 업데이트 실패: {e}")


# ──────────────────────────────────────────────────────────────
# auto_popup_at_2030 — 진입점
# ──────────────────────────────────────────────────────────────

def auto_popup_at_2030(trader=None, parent=None) -> None:
    """
    20:30 NXT 마감 리포트 자동 팝업.

    trading_product.py _check_flow_schedule 에서:
        from nxt_watcher import auto_popup_at_2030
        auto_popup_at_2030(trader=self, parent=self)
    """
    db = trader.db if trader else None

    # DB에서 오늘 nxt_data 조회
    records: list[dict] = []
    if db:
        try:
            today = datetime.date.today().strftime("%Y%m%d")
            cur   = db._conn.cursor()
            cur.execute("SELECT * FROM nxt_data WHERE date = ?", (today,))
            cols  = [d[0] for d in cur.description]
            records = [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            pass

    if not records:
        coll = NXTCollector(trader=trader, db=db)
        try:
            coll._do_save_regular()
            records = coll._build_records()
        except Exception:
            pass

    today   = datetime.date.today().strftime("%Y%m%d")
    now_str = datetime.datetime.now().strftime("%H:%M")
    text    = NXTCollector._format_report(records, today, now_str)
    popup   = NXTReportPopup(text, records, trader=trader, parent=parent)
    popup.exec_()


# ──────────────────────────────────────────────────────────────
# 단독 실행
# ──────────────────────────────────────────────────────────────

def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = NXTWatcher()
    win.setWindowTitle("NXT 감시자")
    win.resize(780, 520)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
