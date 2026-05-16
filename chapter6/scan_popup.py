# -*- coding: utf-8 -*-
"""
14:30 종베 스캐너 팝업 (ScanPopup)

단독 실행:
    python scan_popup.py

외부 호출 (자동 팝업):
    from scan_popup import auto_popup_at_1430
    auto_popup_at_1430(trader=self, parent=self)   # trading_product.py에서

스캔 로직:
    ScanEngine(trader).run_scan(score_min, pullback_min, pullback_max, target_pct, stop_pct)
    → 수급점수 상위 + 눌림 조건 필터 → 진입/목표/손절 계산 → 정렬

DB:
    scan_log 테이블 자동 생성 (db_manager.DBManager 주입 또는 독립 생성)
"""
from __future__ import annotations

import math
import datetime
import sys
import os
from typing import Optional

try:
    from loguru import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

from PyQt5.QtWidgets import (
    QApplication, QDialog, QWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QSpinBox, QDoubleSpinBox,
    QMessageBox, QInputDialog,
)
from PyQt5.QtCore import Qt, QSettings
from PyQt5.QtGui import QColor

_CHAPTER_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_CHAPTER_DIR)
if _CHAPTER_DIR not in sys.path:
    sys.path.insert(0, _CHAPTER_DIR)

try:
    from db_manager import DBManager, DB_PATH
    _DB_OK = True
except ImportError:
    _DB_OK = False

try:
    from flow_monitor import calc_score, calc_temperature
    _FM_OK = True
except ImportError:
    _FM_OK = False
    def calc_score(foreigner, program, vol_ratio, consec_buy=0,
                   prog_accel=False, is_sector_top=False) -> int:
        s = 0
        if foreigner    >  0:   s += 20
        if consec_buy   >= 3:   s += 20
        if program      >  0:   s += 20
        if prog_accel:          s += 15
        if vol_ratio    >= 2.0: s += 15
        if is_sector_top:       s += 10
        return min(s, 100)


# ── scan_log DDL ─────────────────────────────────────────────

_DDL_SCAN_LOG = """
CREATE TABLE IF NOT EXISTS scan_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_date   TEXT NOT NULL,
    scan_time   TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT,
    score       REAL,
    entry       REAL,
    target      REAL,
    stop        REAL,
    rr          REAL,
    pullback    REAL,
    foreigner   REAL,
    program     REAL,
    selected    INTEGER DEFAULT 0,
    result_pct  REAL
);
CREATE INDEX IF NOT EXISTS idx_scan_date ON scan_log(scan_date);
"""


def _ensure_scan_log(conn) -> None:
    """scan_log 테이블이 없으면 자동 생성."""
    try:
        conn.executescript(_DDL_SCAN_LOG)
        conn.commit()
    except Exception:
        pass


# ── 수치 헬퍼 ────────────────────────────────────────────────

def _round100_up(price: float) -> int:
    """100원 단위 올림 (목표가)."""
    return int(math.ceil(price / 100) * 100)


def _round100_down(price: float) -> int:
    """100원 단위 내림 (손절가)."""
    return int(math.floor(price / 100) * 100)


def _round100(price: float) -> int:
    """100원 단위 반올림 (진입가)."""
    return int(round(price / 100) * 100)


# ──────────────────────────────────────────────────────────────
# ScanEngine — 스캔 로직
# ──────────────────────────────────────────────────────────────

class ScanEngine:
    """
    수급점수 + 눌림 조건 필터 → 후보 종목 리스트 반환.

    trader 없으면 데모 데이터로 동작.
    """

    def __init__(self, trader=None) -> None:
        self._trader = trader

    def run_scan(
        self,
        score_min:    int   = 70,
        pullback_min: float = 3.0,
        pullback_max: float = 7.0,
        target_pct:   float = 3.5,
        stop_pct:     float = 3.0,
    ) -> list[dict]:
        if self._trader is None:
            return self._demo_candidates(target_pct, stop_pct)

        try:
            leader_df  = self._trader.sector_analyzer.get_leader_scores()
            summary_df = self._trader.sector_analyzer.get_summary()
        except Exception:
            return self._demo_candidates(target_pct, stop_pct)

        if leader_df is None or leader_df.empty:
            return self._demo_candidates(target_pct, stop_pct)

        # 섹터 순위 맵
        sector_rank_map: dict[str, int] = {}
        if summary_df is not None and not summary_df.empty:
            for rank, (_, row) in enumerate(summary_df.iterrows(), 1):
                sector_rank_map[str(row.get("섹터명", ""))] = rank

        prices = getattr(self._trader, "stock_code_to_realtime_price_dict", {})
        candidates: list[dict] = []

        for _, row in leader_df.iterrows():
            code = str(row.get("종목코드", "")).strip()
            if not code:
                continue

            # 현재가
            price_raw = (prices.get(code)
                         or prices.get(code + "_AL")
                         or row.get("현재가", 0))
            price = float(price_raw or 0)
            if price <= 0:
                continue

            # 수급 데이터
            foreigner_raw = float(row.get("외인순매수", 0) or 0)
            foreigner     = foreigner_raw / 100.0        # → 억원
            program       = float(row.get("프로그램순매수", 0) or 0) / 100.0
            vol_ratio     = float(row.get("거래대금비율", 1.0) or 1.0)
            consec_buy    = int(row.get("외인연속매수", 0) or 0)
            prog_accel    = bool(row.get("프로그램가속", False))
            sector        = str(row.get("섹터명", ""))
            sec_rank      = sector_rank_map.get(sector, 999)
            is_top        = (sec_rank == 1)

            score = calc_score(foreigner, program, vol_ratio,
                               consec_buy, prog_accel, is_top)
            if score < score_min:
                continue

            # 눌림 계산 (당일고가 없으면 스킵)
            high = float(row.get("당일고가", 0) or 0)
            if high > 0 and high >= price:
                pullback = (high - price) / high * 100.0
            else:
                continue   # 고가 데이터 없으면 필터 제외

            if not (pullback_min <= pullback <= pullback_max):
                continue

            change_pct = float(row.get("등락률(%)", 0) or 0)
            volume     = float(row.get("거래대금", 0) or 0) / 100.0   # → 억원

            # 진입/목표/손절
            entry  = _round100(price)
            target = _round100_up(price * (1 + target_pct / 100))
            stop   = _round100_down(price * (1 - stop_pct / 100))
            rr     = round(target_pct / stop_pct, 2) if stop_pct else 0

            # 전일 NXT 갭
            nxt_prev = 0.0
            try:
                recs = self._trader.db.get_nxt_history(code)
                if recs:
                    nxt_prev = float(recs[-1].get("nxt_gap", 0) or 0)
            except Exception:
                pass

            candidates.append({
                "rank":        0,
                "code":        code,
                "name":        str(row.get("종목명", code)),
                "sector":      sector,
                "score":       score,
                "price":       price,
                "change_pct":  change_pct,
                "high":        high,
                "pullback":    pullback,
                "foreigner":   foreigner,
                "program":     program,
                "volume":      volume,
                "fore_days":   consec_buy,
                "prog_accel":  prog_accel,
                "sector_rank": sec_rank,
                "nxt_prev":    nxt_prev,
                "entry":       entry,
                "target":      target,
                "stop":        stop,
                "rr":          rr,
                "target_pct":  target_pct,
                "stop_pct":    stop_pct,
            })

        sorted_c = sorted(candidates, key=lambda x: x["score"], reverse=True)
        for i, c in enumerate(sorted_c):
            c["rank"] = i + 1
        return sorted_c

    # ── 데모 데이터 ──────────────────────────────────────────

    def _demo_candidates(self, target_pct: float = 3.5,
                         stop_pct: float = 3.0) -> list[dict]:
        _demos = [
            {"code": "454910", "name": "두산로보틱스",     "sector": "기계/방산",
             "score": 82, "price": 132100, "change_pct": 23.69, "high": 137900,
             "foreigner": 503.0, "program": 438.0, "volume": 2376.0,
             "fore_days": 3, "prog_accel": True,  "sector_rank": 1, "nxt_prev": 33366.0},
            {"code": "012450", "name": "한화에어로스페이스", "sector": "기계/방산",
             "score": 75, "price":  84200, "change_pct": 15.32, "high":  88100,
             "foreigner": 287.0, "program": 198.0, "volume": 1240.0,
             "fore_days": 2, "prog_accel": True,  "sector_rank": 1, "nxt_prev": 18200.0},
            {"code": "000660", "name": "SK하이닉스",        "sector": "반도체",
             "score": 71, "price": 198500, "change_pct":  4.47, "high": 208000,
             "foreigner": 420.0, "program": 115.0, "volume": 3150.0,
             "fore_days": 1, "prog_accel": False, "sector_rank": 2, "nxt_prev": 12400.0},
        ]
        result = []
        for i, d in enumerate(_demos):
            price = d["price"]
            high  = d["high"]
            result.append({
                **d,
                "rank":       i + 1,
                "pullback":   (high - price) / high * 100.0,
                "entry":      _round100(price),
                "target":     _round100_up(price * (1 + target_pct / 100)),
                "stop":       _round100_down(price * (1 - stop_pct / 100)),
                "rr":         round(target_pct / stop_pct, 2),
                "target_pct": target_pct,
                "stop_pct":   stop_pct,
            })
        return result

    # ── DB 저장 ──────────────────────────────────────────────

    def save_to_db(self, candidates: list[dict], db) -> None:
        if not candidates or db is None:
            return
        conn = db._conn
        _ensure_scan_log(conn)
        now      = datetime.datetime.now()
        date_str = now.strftime("%Y%m%d")
        time_str = now.strftime("%H%M%S")
        try:
            conn.executemany(
                """
                INSERT INTO scan_log
                    (scan_date, scan_time, code, name, score,
                     entry, target, stop, rr, pullback, foreigner, program,
                     selected, result_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                """,
                [
                    (date_str, time_str,
                     c["code"], c["name"], c["score"],
                     c["entry"], c["target"], c["stop"], c["rr"],
                     c["pullback"], c["foreigner"], c["program"])
                    for c in candidates
                ],
            )
            conn.commit()
            logger.info(f"[ScanEngine] scan_log {len(candidates)}건 저장 ({time_str})")
        except Exception as e:
            logger.exception(f"[ScanEngine] DB 저장 실패: {e}")


# ──────────────────────────────────────────────────────────────
# CandidateCard — 후보 종목 카드
# ──────────────────────────────────────────────────────────────

class CandidateCard(QFrame):
    def __init__(self, data: dict, parent=None) -> None:
        super().__init__(parent)
        self._data = data
        self._build()

    @staticmethod
    def _hline() -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#e0e0e0;")
        sep.setFixedHeight(1)
        return sep

    def _build(self) -> None:
        d    = self._data
        rank = d.get("rank", 1)

        if rank == 1:
            border, bg = "#2e7d32", "#f1f8e9"
        elif rank <= 3:
            border, bg = "#90a4ae", "white"
        else:
            border, bg = "#e0e0e0", "#fafafa"

        self.setStyleSheet(
            f"QFrame#card {{ background:{bg}; border:2px solid {border};"
            "border-radius:8px; }}"
        )
        self.setObjectName("card")

        vl = QVBoxLayout(self)
        vl.setContentsMargins(16, 12, 16, 12)
        vl.setSpacing(6)

        # ── 헤더 ─────────────────────────────────────────────
        hl0 = QHBoxLayout()
        rank_badge = QLabel(f"  {rank}위  ")
        rank_badge.setStyleSheet(
            f"background:{border}; color:white; border-radius:3px;"
            "font-size:11px; font-weight:bold;"
        )
        hl0.addWidget(rank_badge)

        name_lbl = QLabel(d["name"])
        name_lbl.setStyleSheet(
            "font-size:15px; font-weight:bold; margin-left:6px;"
        )
        hl0.addWidget(name_lbl)

        sec_lbl = QLabel(d["sector"])
        sec_lbl.setStyleSheet(
            "color:#1565c0; font-size:11px; margin-left:8px;"
        )
        hl0.addWidget(sec_lbl)
        hl0.addStretch()

        sc = d["score"]
        sc_fg = "#2e7d32" if sc >= 70 else ("#f57f17" if sc >= 55 else "#c62828")
        score_lbl = QLabel(f"수급점수: {sc}점")
        score_lbl.setStyleSheet(
            f"font-size:12px; font-weight:bold; color:{sc_fg};"
        )
        hl0.addWidget(score_lbl)
        vl.addLayout(hl0)

        # ── 현재가 ───────────────────────────────────────────
        chg    = d["change_pct"]
        chg_fg = "#c62828" if chg >= 0 else "#1565c0"
        price_lbl = QLabel(
            f"현재가:  {d['price']:,.0f}    등락률:  {chg:+.2f}%"
        )
        price_lbl.setStyleSheet(f"font-size:12px; color:{chg_fg};")
        vl.addWidget(price_lbl)
        vl.addWidget(self._hline())

        # ── 수급 ─────────────────────────────────────────────
        prog_str = (f"{d['program']:+,.0f}억" if d["program"] != 0 else "---")
        supply_lbl = QLabel(
            f"외인순매수:  {d['foreigner']:+,.0f}억    "
            f"프로그램:  {prog_str}"
        )
        supply_lbl.setStyleSheet("font-size:11px; color:#333;")
        vl.addWidget(supply_lbl)

        vol_str = f"{d['volume']:,.0f}억" if d["volume"] > 0 else "---"
        pull_lbl = QLabel(
            f"고점대비눌림:  {d['pullback']:.1f}%    거래대금:  {vol_str}"
        )
        pull_lbl.setStyleSheet("font-size:11px; color:#333;")
        vl.addWidget(pull_lbl)
        vl.addWidget(self._hline())

        # ── 진입/목표/손절 ────────────────────────────────────
        entry_lbl = QLabel(f"진입가:  {d['entry']:,}  (현재가 기준)")
        entry_lbl.setStyleSheet("font-size:11px; color:#333;")
        vl.addWidget(entry_lbl)

        target_lbl = QLabel(
            f"목표가:  {d['target']:,}  (+{d['target_pct']:.1f}%)  🎯"
        )
        target_lbl.setStyleSheet(
            "font-size:11px; font-weight:bold; color:#2e7d32;"
        )
        vl.addWidget(target_lbl)

        stop_lbl = QLabel(
            f"손절가:  {d['stop']:,}  (-{d['stop_pct']:.1f}%)  🛑"
        )
        stop_lbl.setStyleSheet(
            "font-size:11px; font-weight:bold; color:#c62828;"
        )
        vl.addWidget(stop_lbl)

        rr_lbl = QLabel(f"예상 리스크/리워드:  1 : {d['rr']:.2f}")
        rr_lbl.setStyleSheet("font-size:11px; font-weight:bold; color:#333;")
        vl.addWidget(rr_lbl)
        vl.addWidget(self._hline())

        # ── 수급 상세 ─────────────────────────────────────────
        detail_hdr = QLabel("수급 상세:")
        detail_hdr.setStyleSheet("font-size:11px; font-weight:bold; color:#333;")
        vl.addWidget(detail_hdr)

        accel_str = "상승 중" if d["prog_accel"] else "보통"
        detail1 = QLabel(
            f"외인연속:  {d['fore_days']}일    프로그램가속:  {accel_str}"
        )
        detail1.setStyleSheet("font-size:11px; color:#555;")
        vl.addWidget(detail1)

        nxt_str = (f"{d['nxt_prev']:+,.0f}백만" if d["nxt_prev"] != 0 else "없음")
        detail2 = QLabel(
            f"업종순위:  {d['sector_rank']}위    NXT전일:  {nxt_str}"
        )
        detail2.setStyleSheet("font-size:11px; color:#555;")
        vl.addWidget(detail2)
        vl.addWidget(self._hline())

        # ── 버튼 ─────────────────────────────────────────────
        btn_hl = QHBoxLayout()
        btn_hl.addStretch()

        watch_btn = QPushButton("관심등록")
        watch_btn.setFixedSize(90, 28)
        watch_btn.setStyleSheet(
            "background:#e3f2fd; color:#1565c0; border:1px solid #90caf9;"
            "border-radius:4px; font-size:11px;"
        )
        watch_btn.clicked.connect(self._on_watchlist)
        btn_hl.addWidget(watch_btn)

        log_btn = QPushButton("매매일지 기록")
        log_btn.setFixedSize(100, 28)
        log_btn.setStyleSheet(
            "background:#f3e5f5; color:#6a1b9a; border:1px solid #ce93d8;"
            "border-radius:4px; font-size:11px;"
        )
        log_btn.clicked.connect(self._on_trade_log)
        btn_hl.addWidget(log_btn)

        btn_hl.addStretch()
        vl.addLayout(btn_hl)

    def _on_watchlist(self) -> None:
        d = self._data
        QMessageBox.information(
            self, "관심종목",
            f"{d['name']} ({d['code']}) 관심종목 등록\n"
            "※ 키움 API 관심종목 등록은 추후 구현 예정",
        )

    def _on_trade_log(self) -> None:
        d = self._data
        memo, ok = QInputDialog.getText(
            self, "매매일지",
            f"{d['name']} 메모:",
            text=(
                f"종베 진입 | 진입가:{d['entry']:,} "
                f"목표:{d['target']:,} 손절:{d['stop']:,}"
            ),
        )
        if ok and memo:
            QMessageBox.information(self, "저장완료", f"메모 기록: {memo[:40]}")


# ──────────────────────────────────────────────────────────────
# ScanPopup — 메인 팝업 다이얼로그
# ──────────────────────────────────────────────────────────────

class ScanPopup(QDialog):
    """
    14:30 종베 스캐너 팝업.

    ScanPopup(candidates, trader=trader_instance, parent=parent).exec_()
    """

    def __init__(
        self,
        candidates: list[dict],
        trader=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._candidates = candidates
        self._trader     = trader
        self._settings   = QSettings("StockCoding", "ScanPopup")
        self._scan_time  = datetime.datetime.now()

        self.setWindowTitle("🎯 14:30 종베 스캐너")
        self.setMinimumSize(660, 700)
        self.resize(700, 820)
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowMaximizeButtonHint
        )

        self._build_ui()
        self._load_settings()
        self._refresh_cards()

    # ── UI 구성 ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        # 타이틀 바
        title_bar = QWidget()
        title_bar.setStyleSheet("background:#1a237e;")
        title_bar.setFixedHeight(46)
        hl_t = QHBoxLayout(title_bar)
        hl_t.setContentsMargins(14, 0, 14, 0)

        title_lbl = QLabel("🎯  14:30 종베 스캐너")
        title_lbl.setStyleSheet(
            "color:white; font-size:15px; font-weight:bold;"
        )
        hl_t.addWidget(title_lbl)
        hl_t.addStretch()

        self._time_lbl = QLabel(
            f"스캔시각: {self._scan_time.strftime('%H:%M:%S')}"
        )
        self._time_lbl.setStyleSheet("color:#bbdefb; font-size:12px;")
        hl_t.addWidget(self._time_lbl)

        self._cnt_lbl = QLabel(f"후보 {len(self._candidates)}종목")
        self._cnt_lbl.setStyleSheet(
            "background:#e53935; color:white; border-radius:3px;"
            "font-size:12px; font-weight:bold; padding:2px 8px; margin-left:10px;"
        )
        hl_t.addWidget(self._cnt_lbl)
        vl.addWidget(title_bar)

        # 콘텐츠
        content = QWidget()
        cvl = QVBoxLayout(content)
        cvl.setContentsMargins(12, 10, 12, 6)
        cvl.setSpacing(8)

        # 섹션 1: 필터 바
        cvl.addWidget(self._build_filter_bar())

        # 섹션 2: 카드 스크롤
        self._scroll     = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("QScrollArea { border:none; }")
        self._card_w     = QWidget()
        self._card_vl    = QVBoxLayout(self._card_w)
        self._card_vl.setSpacing(10)
        self._card_vl.setContentsMargins(2, 2, 2, 2)
        self._card_vl.addStretch()
        self._scroll.setWidget(self._card_w)
        cvl.addWidget(self._scroll, 1)

        # 섹션 3: 요약 바
        self._sum_bar = self._build_summary_bar()
        cvl.addWidget(self._sum_bar)

        vl.addWidget(content, 1)

        # 버튼 바 (최하단)
        vl.addWidget(self._build_button_bar())

    def _build_filter_bar(self) -> QFrame:
        bar = QFrame()
        bar.setStyleSheet(
            "QFrame { background:#f8f9fa; border:1px solid #dee2e6;"
            "border-radius:6px; }"
        )
        bar.setFixedHeight(44)
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(12, 4, 12, 4)
        hl.setSpacing(6)

        def _spin(min_v, max_v, val, suf, w=72):
            s = QSpinBox() if isinstance(val, int) else QDoubleSpinBox()
            s.setRange(min_v, max_v)
            s.setValue(val)
            s.setSuffix(suf)
            s.setFixedWidth(w)
            s.setButtonSymbols(QSpinBox.NoButtons)
            return s

        def _add(label, widget):
            hl.addWidget(QLabel(label))
            hl.addWidget(widget)

        self._score_spin   = _spin(50, 100, 70, "점",  68)
        self._pull_min     = _spin(0.0, 20.0, 3.0, "%", 60)
        self._pull_max     = _spin(0.0, 30.0, 7.0, "%", 60)
        self._target_spin  = _spin(0.5, 20.0, 3.5, "%", 60)
        self._stop_spin    = _spin(0.5, 10.0, 3.0, "%", 60)

        _add("수급점수:", self._score_spin)
        hl.addWidget(_sep())
        _add("눌림 최소:", self._pull_min)
        _add("최대:", self._pull_max)
        hl.addWidget(_sep())
        _add("목표수익:", self._target_spin)
        _add("손절:", self._stop_spin)
        hl.addStretch()

        rescan_btn = QPushButton("재스캔")
        rescan_btn.setFixedSize(64, 30)
        rescan_btn.setStyleSheet(
            "background:#1565c0; color:white; border-radius:4px;"
            "font-size:12px; font-weight:bold;"
        )
        rescan_btn.clicked.connect(self._on_rescan)
        hl.addWidget(rescan_btn)
        return bar

    def _build_summary_bar(self) -> QFrame:
        bar = QFrame()
        bar.setStyleSheet(
            "QFrame { background:#f5f5f5; border-top:1px solid #e0e0e0; }"
        )
        bar.setFixedHeight(36)
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(12, 4, 12, 4)
        hl.setSpacing(20)

        self._stat_sector = QLabel("오늘 업종 1위: --")
        self._stat_nxt    = QLabel("NXT 전일합: --")
        self._stat_top    = QLabel("종베 추천: --")

        for lbl in (self._stat_sector, self._stat_nxt, self._stat_top):
            lbl.setStyleSheet("font-size:11px; color:#555;")
            hl.addWidget(lbl)
        hl.addStretch()
        return bar

    def _build_button_bar(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet("background:#eeeeee; border-top:1px solid #ccc;")
        bar.setFixedHeight(48)
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(12, 8, 12, 8)
        hl.setSpacing(8)

        save_btn = QPushButton("전체 매매일지 저장")
        save_btn.setFixedHeight(32)
        save_btn.setStyleSheet(
            "background:#1565c0; color:white; border-radius:4px; font-size:12px;"
        )
        save_btn.clicked.connect(self._on_save_all)
        hl.addWidget(save_btn)

        alert_btn = QPushButton("내일 알림 설정")
        alert_btn.setFixedHeight(32)
        alert_btn.setStyleSheet(
            "background:#2e7d32; color:white; border-radius:4px; font-size:12px;"
        )
        alert_btn.clicked.connect(self._on_tomorrow_alert)
        hl.addWidget(alert_btn)

        hl.addStretch()

        close_btn = QPushButton("닫기")
        close_btn.setFixedSize(70, 32)
        close_btn.setStyleSheet(
            "background:#757575; color:white; border-radius:4px; font-size:12px;"
        )
        close_btn.clicked.connect(self.close)
        hl.addWidget(close_btn)
        return bar

    # ── 카드 갱신 ────────────────────────────────────────────

    def _refresh_cards(self) -> None:
        # 기존 카드 전부 제거 (마지막 stretch 유지)
        while self._card_vl.count() > 1:
            item = self._card_vl.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._candidates:
            empty = QLabel(
                "조건 충족 종목 없음\n수급점수 기준을 낮추거나 눌림 범위를 조정해보세요."
            )
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(
                "font-size:14px; color:#888; padding:60px;"
            )
            self._card_vl.insertWidget(0, empty)
        else:
            for i, c in enumerate(self._candidates):
                self._card_vl.insertWidget(i, CandidateCard(c))

        self._cnt_lbl.setText(f"후보 {len(self._candidates)}종목")
        self._update_summary()

    def _update_summary(self) -> None:
        if not self._candidates:
            return
        top = self._candidates[0]

        # 업종 1위
        top1_sector = next(
            (c["sector"] for c in self._candidates if c["sector_rank"] == 1),
            top["sector"],
        )
        self._stat_sector.setText(
            f"오늘 업종 1위: {top1_sector} (온도점수 {top['score']})"
        )

        # NXT 전일합
        nxt_total = sum(c["nxt_prev"] for c in self._candidates)
        if nxt_total:
            self._stat_nxt.setText(f"NXT 전일 총합: {nxt_total:+,.0f}백만")

        # 종베 추천
        self._stat_top.setText(f"종베 추천: {top['name']} (최고점수)")

    # ── 이벤트 핸들러 ────────────────────────────────────────

    def _on_rescan(self) -> None:
        self._save_settings()
        score_min   = self._score_spin.value()
        pull_min    = self._pull_min.value()
        pull_max    = self._pull_max.value()
        target_pct  = self._target_spin.value()
        stop_pct    = self._stop_spin.value()

        engine = ScanEngine(self._trader)
        self._candidates = engine.run_scan(
            score_min, pull_min, pull_max, target_pct, stop_pct
        )
        self._time_lbl.setText(
            f"스캔시각: {datetime.datetime.now().strftime('%H:%M:%S')}"
        )
        self._refresh_cards()

    def _on_save_all(self) -> None:
        if not self._candidates:
            QMessageBox.warning(self, "저장", "저장할 후보 종목이 없습니다.")
            return
        db = getattr(self._trader, "db", None) if self._trader else None
        if db is None and _DB_OK:
            db = DBManager()
        engine = ScanEngine(self._trader)
        engine.save_to_db(self._candidates, db)
        QMessageBox.information(
            self, "저장완료",
            f"{len(self._candidates)}개 종목을 scan_log에 저장했습니다.\n"
            f"경로: {getattr(db, 'db_path', DB_PATH if _DB_OK else '알 수 없음')}",
        )

    def _on_tomorrow_alert(self) -> None:
        if not self._candidates:
            QMessageBox.information(self, "알림", "후보 종목이 없습니다.")
            return
        names = [c["name"] for c in self._candidates[:3]]
        QMessageBox.information(
            self, "내일 알림 설정",
            "내일 09:00 모니터링 알림 등록 예정:\n"
            + "\n".join(f"  • {n}" for n in names)
            + "\n\n※ 키움 알림 연동은 추후 구현 예정",
        )

    # ── 설정 저장/불러오기 ────────────────────────────────────

    def _save_settings(self) -> None:
        s = self._settings
        s.setValue("score_min",    self._score_spin.value())
        s.setValue("pullback_min", self._pull_min.value())
        s.setValue("pullback_max", self._pull_max.value())
        s.setValue("target_pct",   self._target_spin.value())
        s.setValue("stop_pct",     self._stop_spin.value())

    def _load_settings(self) -> None:
        s = self._settings
        self._score_spin.setValue(s.value("score_min",    70,  int))
        self._pull_min.setValue(  s.value("pullback_min", 3.0, float))
        self._pull_max.setValue(  s.value("pullback_max", 7.0, float))
        self._target_spin.setValue(s.value("target_pct",  3.5, float))
        self._stop_spin.setValue(  s.value("stop_pct",    3.0, float))

    def closeEvent(self, event) -> None:
        self._save_settings()
        super().closeEvent(event)


# ──────────────────────────────────────────────────────────────
# 공통 위젯 헬퍼
# ──────────────────────────────────────────────────────────────

def _sep() -> QLabel:
    lbl = QLabel("|")
    lbl.setStyleSheet("color:#ccc; font-size:14px;")
    return lbl


# ──────────────────────────────────────────────────────────────
# auto_popup_at_1430 — 외부 호출용 진입점
# ──────────────────────────────────────────────────────────────

def auto_popup_at_1430(trader=None, parent=None) -> None:
    """
    14:30 자동 팝업 진입점.

    trading_product.py _check_flow_schedule() 에서 호출:
        from scan_popup import auto_popup_at_1430
        auto_popup_at_1430(trader=self, parent=self)
    """
    s            = QSettings("StockCoding", "ScanPopup")
    score_min    = s.value("score_min",    70,  int)
    pullback_min = s.value("pullback_min", 3.0, float)
    pullback_max = s.value("pullback_max", 7.0, float)
    target_pct   = s.value("target_pct",  3.5, float)
    stop_pct     = s.value("stop_pct",    3.0, float)

    engine     = ScanEngine(trader)
    candidates = engine.run_scan(
        score_min, pullback_min, pullback_max, target_pct, stop_pct
    )

    if not candidates:
        QMessageBox.information(
            parent, "종베 스캐너",
            "14:30 스캔 — 조건 충족 종목 없음\n"
            "수급점수 기준을 낮추거나 눌림 범위를 조정해보세요.",
        )
        return

    popup = ScanPopup(candidates, trader=trader, parent=parent)
    popup.exec_()


# ──────────────────────────────────────────────────────────────
# 단독 실행 (테스트)
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    # trader=None → 데모 데이터로 동작
    popup = ScanPopup(ScanEngine().run_scan(), trader=None)
    popup.show()
    sys.exit(app.exec_())
