# -*- coding: utf-8 -*-
"""
stock_flow_db.py — 종목별 수급 흐름 SQLite 영속성 레이어

스냅샷 단위: 배치 완료마다 (기본 2분) 실제 시각(분 단위)으로 저장.
재시작 후 오늘 날짜 스냅샷을 로드하여 히스토리를 복원.

시간 범위: 08:00 ~ 20:00 (분 단위 동적 슬롯)
"""
from __future__ import annotations

import datetime
import os
import sqlite3
import threading
from typing import Optional

import pandas as pd

# 하위 호환용 — widget이 import 해도 빈 리스트로 처리됨
TIME_SLOTS: list[str] = []

_DB_DIR  = os.path.join(os.path.dirname(__file__), "..", "data")
_DB_PATH = os.path.join(_DB_DIR, "stock_flow.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS stock_flow_snapshots (
    date        TEXT NOT NULL,
    time        TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    sector      TEXT NOT NULL DEFAULT '',
    foreign_net REAL NOT NULL DEFAULT 0,
    inst_net    REAL NOT NULL DEFAULT 0,
    fin_net     REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, time, code)
);
CREATE INDEX IF NOT EXISTS idx_sfsnap_date ON stock_flow_snapshots (date);
"""


class StockFlowDB:
    """종목별 수급 흐름 스냅샷 DB (thread-safe)."""

    def __init__(self, db_path: str = _DB_PATH) -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._path = db_path
        self._lock = threading.Lock()
        self._init_db()

    # ── 초기화 ────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_CREATE_SQL)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    # ── 저장 ─────────────────────────────────────────────────

    def save_snapshot(
        self,
        stock_rows: list[dict],
        snap_time: Optional[str] = None,
        snap_date: Optional[str] = None,
    ) -> int:
        """
        종목 수급 스냅샷을 저장.

        Args:
            stock_rows: [{"code", "name", "sector",
                          "foreign_net", "inst_net", "fin_net"}, ...]
            snap_time:  "HH:MM" — None이면 현재 시각 1분 버킷
            snap_date:  "YYYYMMDD" — None이면 오늘

        Returns:
            저장된 행 수
        """
        if not stock_rows:
            return 0

        now = datetime.datetime.now()
        if snap_date is None:
            snap_date = now.strftime("%Y%m%d")
        if snap_time is None:
            # 1분 버킷 — 실제 분 단위로 저장
            snap_time = f"{now.hour:02d}:{now.minute:02d}"
            # 08:00 ~ 20:00 범위 클램핑
            if snap_time < "08:00":
                snap_time = "08:00"
            elif snap_time > "20:00":
                snap_time = "20:00"

        rows = [
            (
                snap_date,
                snap_time,
                str(r.get("code", "")).strip(),
                str(r.get("name", "")).strip(),
                str(r.get("sector", "")).strip(),
                float(r.get("foreign_net", 0) or 0),
                float(r.get("inst_net",    0) or 0),
                float(r.get("fin_net",     0) or 0),
            )
            for r in stock_rows
            if str(r.get("code", "")).strip()
        ]
        if not rows:
            return 0

        sql = """
            INSERT OR REPLACE INTO stock_flow_snapshots
                (date, time, code, name, sector, foreign_net, inst_net, fin_net)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._lock:
            with self._connect() as conn:
                conn.executemany(sql, rows)
        return len(rows)

    # ── 로드 ─────────────────────────────────────────────────

    def load_today(self, date: Optional[str] = None) -> pd.DataFrame:
        """
        오늘(또는 지정 날짜) 전체 스냅샷을 DataFrame으로 반환.

        컬럼: date, time, code, name, sector,
              foreign_net, inst_net, fin_net
        """
        if date is None:
            date = datetime.datetime.now().strftime("%Y%m%d")

        sql = """
            SELECT date, time, code, name, sector,
                   foreign_net, inst_net, fin_net
            FROM stock_flow_snapshots
            WHERE date = ?
            ORDER BY time, code
        """
        with self._lock:
            with self._connect() as conn:
                df = pd.read_sql_query(sql, conn, params=(date,))
        return df

    def available_dates(self) -> list[str]:
        """저장된 날짜 목록 (최신순)."""
        sql = "SELECT DISTINCT date FROM stock_flow_snapshots ORDER BY date DESC"
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(sql).fetchall()
        return [r["date"] for r in rows]

    def delete_date(self, date: Optional[str] = None) -> int:
        """특정 날짜 데이터 전체 삭제. date=None이면 오늘."""
        if date is None:
            date = datetime.datetime.now().strftime("%Y%m%d")
        sql = "DELETE FROM stock_flow_snapshots WHERE date = ?"
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(sql, (date,))
                return cur.rowcount

    def delete_before(self, days: int = 30) -> int:
        """days일 이전 데이터 삭제."""
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y%m%d")
        sql = "DELETE FROM stock_flow_snapshots WHERE date < ?"
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(sql, (cutoff,))
                return cur.rowcount

    # ── 피벗 헬퍼 ────────────────────────────────────────────

    @staticmethod
    def pivot_for_widget(df: pd.DataFrame) -> pd.DataFrame:
        """
        load_today() 결과를 위젯용 피벗 테이블로 변환.

        반환 DataFrame:
            고정 컬럼: code, name, sector, investor_type
            시간 컬럼: DB에 실제 저장된 시각(분 단위, 동적)
            값:        해당 시각의 순매수 (없으면 NaN)

        investor_type: '외인' / '기관' / '금투'
        """
        if df.empty:
            return pd.DataFrame(columns=["code", "name", "sector", "investor_type"])

        # DB에 실제 존재하는 시각 목록 (정렬)
        actual_times = sorted(df["time"].unique().tolist())

        records = []
        for code, grp in df.groupby("code"):
            latest = grp.sort_values("time", ascending=False)
            name   = next((v for v in latest["name"].tolist()   if v), "")
            sector = next((v for v in latest["sector"].tolist() if v), "")

            time_map = {row["time"]: row for _, row in grp.iterrows()}

            for inv_type, field in [("프로그램", "foreign_net"), ("기관", "inst_net"), ("금투", "fin_net")]:
                row_data = {
                    "code":          code,
                    "name":          name,
                    "sector":        sector,
                    "investor_type": inv_type,
                }
                for t in actual_times:
                    snap = time_map.get(t)
                    row_data[t] = float(snap[field]) if snap is not None else float("nan")
                records.append(row_data)

        result = pd.DataFrame(records)
        result = result.sort_values(["sector", "code", "investor_type"]).reset_index(drop=True)
        return result
