# -*- coding: utf-8 -*-
"""
DBManager — SQLite 영속성 모듈

테이블:
  supply_flow  : 1분 단위 수급흐름 스냅샷
  trade_log    : 진입/청산 매매일지
  nxt_data     : NXT 장후 수급 (20:00 저장)
  holding      : 현재 보유종목 실시간 유지
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import datetime
from typing import Optional

from loguru import logger


# ──────────────────────────────────────────────────────────────
# 경로 설정
# ──────────────────────────────────────────────────────────────

_CHAPTER_DIR = os.path.dirname(os.path.abspath(__file__))   # .../chapter6/
_PROJECT_DIR = os.path.dirname(_CHAPTER_DIR)                 # .../OpenAPI/
DB_PATH      = os.path.join(_PROJECT_DIR, "data", "trade_data.db")


# ──────────────────────────────────────────────────────────────
# DDL
# ──────────────────────────────────────────────────────────────

_DDL_SUPPLY = """
CREATE TABLE IF NOT EXISTS supply_flow (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT    NOT NULL,
    time          TEXT    NOT NULL,
    code          TEXT    NOT NULL,
    name          TEXT,
    sector        TEXT,
    foreigner_buy REAL,
    program_buy   REAL,
    volume        REAL,
    price         REAL,
    source        TEXT    DEFAULT 'regular'
);
CREATE INDEX IF NOT EXISTS idx_supply_code_date ON supply_flow(code, date);
"""

_DDL_TRADE = """
CREATE TABLE IF NOT EXISTS trade_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,
    code        TEXT    NOT NULL,
    name        TEXT,
    entry_price REAL,
    entry_time  TEXT,
    exit_price  REAL,
    exit_time   TEXT,
    pnl_pct     REAL,
    hold_days   INTEGER,
    entry_cond  TEXT,
    exit_reason TEXT,
    score       REAL
);
CREATE INDEX IF NOT EXISTS idx_trade_code ON trade_log(code);
"""

_DDL_NXT = """
CREATE TABLE IF NOT EXISTS nxt_data (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT    NOT NULL,
    code         TEXT    NOT NULL,
    name         TEXT,
    regular_fore REAL,
    nxt_fore     REAL,
    nxt_gap      REAL,
    verdict      TEXT,
    next_open    REAL,
    next_pct     REAL
);
CREATE INDEX IF NOT EXISTS idx_nxt_code_date ON nxt_data(code, date);
"""

_DDL_HOLDING = """
CREATE TABLE IF NOT EXISTS holding (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT    NOT NULL UNIQUE,
    name          TEXT,
    sector        TEXT,
    entry_date    TEXT,
    entry_price   REAL,
    current_price REAL,
    pnl_pct       REAL,
    score         REAL,
    nxt_status    TEXT,
    hold_days     INTEGER,
    status        TEXT    DEFAULT '보유'
);
"""

_DDL_SCAN = """
CREATE TABLE IF NOT EXISTS scan_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_date   TEXT    NOT NULL,
    scan_time   TEXT    NOT NULL,
    code        TEXT    NOT NULL,
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


class DBManager:
    """SQLite 기반 영속성 관리 클래스."""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous  = NORMAL")
        self._create_tables()
        logger.info(f"[DB] 연결 완료: {db_path}")

    # ── 초기화 ────────────────────────────────────────────────

    def _create_tables(self) -> None:
        cur = self._conn.cursor()
        for ddl in (_DDL_SUPPLY, _DDL_TRADE, _DDL_NXT, _DDL_HOLDING, _DDL_SCAN):
            cur.executescript(ddl)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """기존 DB 스키마 마이그레이션 — 누락 컬럼 자동 추가."""
        migrations: list[tuple[str, str, str]] = [
            # (테이블명, 컬럼명, 컬럼 정의)
            ("supply_flow", "name",          "TEXT"),
            ("supply_flow", "sector",        "TEXT"),
            ("supply_flow", "foreigner_buy", "REAL"),
            ("supply_flow", "program_buy",   "REAL"),
            ("supply_flow", "volume",        "REAL"),
            ("supply_flow", "price",         "REAL"),
            ("supply_flow", "source",        "TEXT DEFAULT 'regular'"),
        ]
        cur = self._conn.cursor()
        for table, col, col_def in migrations:
            cur.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in cur.fetchall()}
            if col not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
                logger.info(f"[DB] 마이그레이션: {table}.{col} 컬럼 추가")
        self._conn.commit()

    # ── 저장 ──────────────────────────────────────────────────

    def save_supply(self, data: dict) -> None:
        """supply_flow 행 추가.

        필수 키: date, time, code
        선택 키: name, sector, foreigner_buy, program_buy, volume, price, source
        """
        row = {
            "date":          data.get("date", ""),
            "time":          data.get("time", ""),
            "code":          data.get("code", ""),
            "name":          data.get("name"),
            "sector":        data.get("sector"),
            "foreigner_buy": data.get("foreigner_buy"),
            "program_buy":   data.get("program_buy"),
            "volume":        data.get("volume"),
            "price":         data.get("price"),
            "source":        data.get("source", "regular"),
        }
        sql = """
        INSERT INTO supply_flow
            (date, time, code, name, sector,
             foreigner_buy, program_buy, volume, price, source)
        VALUES
            (:date, :time, :code, :name, :sector,
             :foreigner_buy, :program_buy, :volume, :price, :source)
        """
        try:
            self._conn.execute(sql, row)
            self._conn.commit()
        except Exception as e:
            logger.exception(f"[DB] save_supply 실패: {e}")

    def save_supply_batch(self, rows: list[dict]) -> None:
        """supply_flow 다중 행 배치 저장 (1분 스냅샷용)."""
        if not rows:
            return
        sql = """
        INSERT INTO supply_flow
            (date, time, code, name, sector,
             foreigner_buy, program_buy, volume, price, source)
        VALUES
            (:date, :time, :code, :name, :sector,
             :foreigner_buy, :program_buy, :volume, :price, :source)
        """
        try:
            self._conn.executemany(sql, rows)
            self._conn.commit()
        except Exception as e:
            logger.exception(f"[DB] save_supply_batch 실패: {e}")

    def save_trade(self, data: dict) -> None:
        """trade_log 행 추가.

        entry_cond가 dict이면 JSON 직렬화 후 저장.
        """
        row = dict(data)
        if isinstance(row.get("entry_cond"), dict):
            row["entry_cond"] = json.dumps(row["entry_cond"], ensure_ascii=False)
        sql = """
        INSERT INTO trade_log
            (date, code, name, entry_price, entry_time,
             exit_price, exit_time, pnl_pct, hold_days,
             entry_cond, exit_reason, score)
        VALUES
            (:date, :code, :name, :entry_price, :entry_time,
             :exit_price, :exit_time, :pnl_pct, :hold_days,
             :entry_cond, :exit_reason, :score)
        """
        try:
            self._conn.execute(sql, row)
            self._conn.commit()
        except Exception as e:
            logger.exception(f"[DB] save_trade 실패: {e}")

    def save_nxt(self, data: dict) -> None:
        """nxt_data 행 추가. 같은 날짜+종목이면 UPDATE."""
        row = {
            "date":         data.get("date", ""),
            "code":         data.get("code", ""),
            "name":         data.get("name"),
            "regular_fore": data.get("regular_fore"),
            "nxt_fore":     data.get("nxt_fore"),
            "nxt_gap":      data.get("nxt_gap"),
            "verdict":      data.get("verdict"),
            "next_open":    data.get("next_open"),
            "next_pct":     data.get("next_pct"),
        }
        sql = """
        INSERT INTO nxt_data
            (date, code, name, regular_fore, nxt_fore, nxt_gap, verdict, next_open, next_pct)
        VALUES
            (:date, :code, :name, :regular_fore, :nxt_fore, :nxt_gap, :verdict, :next_open, :next_pct)
        ON CONFLICT(rowid) DO NOTHING
        """
        # date+code 중복 처리: 같은 날 같은 종목은 UPDATE
        upsert_sql = """
        INSERT INTO nxt_data
            (date, code, name, regular_fore, nxt_fore, nxt_gap, verdict, next_open, next_pct)
        VALUES
            (:date, :code, :name, :regular_fore, :nxt_fore, :nxt_gap, :verdict, :next_open, :next_pct)
        ON CONFLICT DO NOTHING
        """
        try:
            # 같은 날짜+종목 이미 존재하면 UPDATE (next_open/next_pct 제외)
            exists = self._conn.execute(
                "SELECT id FROM nxt_data WHERE date=? AND code=?",
                (row["date"], row["code"])
            ).fetchone()
            if exists:
                self._conn.execute(
                    """UPDATE nxt_data SET name=:name, regular_fore=:regular_fore,
                       nxt_fore=:nxt_fore, nxt_gap=:nxt_gap, verdict=:verdict
                       WHERE date=:date AND code=:code""",
                    row,
                )
            else:
                self._conn.execute(
                    """INSERT INTO nxt_data
                       (date, code, name, regular_fore, nxt_fore, nxt_gap, verdict, next_open, next_pct)
                       VALUES (:date, :code, :name, :regular_fore, :nxt_fore, :nxt_gap, :verdict,
                               :next_open, :next_pct)""",
                    row,
                )
            self._conn.commit()
        except Exception as e:
            logger.exception(f"[DB] save_nxt 실패: {e}")

    def update_nxt_next_day(self, date: str, code: str,
                            next_open: float, next_pct: float) -> None:
        """nxt_data의 next_open / next_pct 업데이트 (다음날 시가/등락률 확인 후)."""
        try:
            self._conn.execute(
                "UPDATE nxt_data SET next_open=?, next_pct=? WHERE date=? AND code=?",
                (next_open, next_pct, date, code),
            )
            self._conn.commit()
        except Exception as e:
            logger.exception(f"[DB] update_nxt_next_day 실패: {e}")

    def update_holding(self, code: str, data: dict) -> None:
        """holding UPSERT — code 기준 삽입 또는 전체 업데이트."""
        row = {
            "code":          code,
            "name":          data.get("name"),
            "sector":        data.get("sector"),
            "entry_date":    data.get("entry_date"),
            "entry_price":   data.get("entry_price"),
            "current_price": data.get("current_price"),
            "pnl_pct":       data.get("pnl_pct"),
            "score":         data.get("score"),
            "nxt_status":    data.get("nxt_status"),
            "hold_days":     data.get("hold_days"),
            "status":        data.get("status", "보유"),
        }
        sql = """
        INSERT INTO holding
            (code, name, sector, entry_date, entry_price,
             current_price, pnl_pct, score, nxt_status, hold_days, status)
        VALUES
            (:code, :name, :sector, :entry_date, :entry_price,
             :current_price, :pnl_pct, :score, :nxt_status, :hold_days, :status)
        ON CONFLICT(code) DO UPDATE SET
            name          = excluded.name,
            sector        = excluded.sector,
            entry_date    = excluded.entry_date,
            entry_price   = excluded.entry_price,
            current_price = excluded.current_price,
            pnl_pct       = excluded.pnl_pct,
            score         = excluded.score,
            nxt_status    = excluded.nxt_status,
            hold_days     = excluded.hold_days,
            status        = excluded.status
        """
        try:
            self._conn.execute(sql, row)
            self._conn.commit()
        except Exception as e:
            logger.exception(f"[DB] update_holding 실패: {e}")

    def remove_holding(self, code: str) -> None:
        """보유종목에서 제거."""
        try:
            self._conn.execute("DELETE FROM holding WHERE code = ?", (code,))
            self._conn.commit()
        except Exception as e:
            logger.exception(f"[DB] remove_holding 실패: {e}")

    def clear_holding(self) -> None:
        """보유종목 전체 삭제."""
        try:
            self._conn.execute("DELETE FROM holding")
            self._conn.commit()
        except Exception as e:
            logger.exception(f"[DB] clear_holding 실패: {e}")

    # ── 조회 ──────────────────────────────────────────────────

    def get_holding_list(self) -> list[dict]:
        """보유종목 전체 반환 (진입일 오름차순)."""
        try:
            cur = self._conn.execute(
                "SELECT * FROM holding ORDER BY entry_date ASC"
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            logger.exception(f"[DB] get_holding_list 실패: {e}")
            return []

    def get_supply_today(self, code: str) -> list[dict]:
        """오늘 수급흐름 조회 (시간 오름차순)."""
        today = datetime.date.today().strftime("%Y%m%d")
        try:
            cur = self._conn.execute(
                "SELECT * FROM supply_flow WHERE code=? AND date=? ORDER BY time ASC",
                (code, today),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            logger.exception(f"[DB] get_supply_today 실패: {e}")
            return []

    def get_trade_history(self, days: int = 30) -> list[dict]:
        """최근 N일 매매이력 반환 (최신순). entry_cond는 dict로 역직렬화."""
        cutoff = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y%m%d")
        try:
            cur = self._conn.execute(
                "SELECT * FROM trade_log WHERE date >= ? ORDER BY date DESC, id DESC",
                (cutoff,),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            for r in rows:
                if r.get("entry_cond"):
                    try:
                        r["entry_cond"] = json.loads(r["entry_cond"])
                    except Exception:
                        pass
            return rows
        except Exception as e:
            logger.exception(f"[DB] get_trade_history 실패: {e}")
            return []

    def get_nxt_history(self, code: str) -> list[dict]:
        """종목별 NXT 이력 반환 (최신순)."""
        try:
            cur = self._conn.execute(
                "SELECT * FROM nxt_data WHERE code=? ORDER BY date DESC",
                (code,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            logger.exception(f"[DB] get_nxt_history 실패: {e}")
            return []

    def get_nxt_yesterday(self) -> list[dict]:
        """전일 nxt_data 전체 반환 (next_open 업데이트 대상 조회용)."""
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y%m%d")
        try:
            cur = self._conn.execute(
                "SELECT * FROM nxt_data WHERE date=? AND next_open IS NULL",
                (yesterday,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            logger.exception(f"[DB] get_nxt_yesterday 실패: {e}")
            return []

    def get_trade_stats(self, days: int = 30) -> dict:
        """최근 N일 매매 통계 반환.

        Returns:
            total, wins, losses, avg_pnl, win_rate, max_pnl, min_pnl
        """
        rows = self.get_trade_history(days)
        if not rows:
            return {"total": 0, "wins": 0, "losses": 0,
                    "avg_pnl": 0.0, "win_rate": 0.0,
                    "max_pnl": 0.0, "min_pnl": 0.0}
        pnls    = [r["pnl_pct"] for r in rows if r.get("pnl_pct") is not None]
        wins    = sum(1 for p in pnls if p > 0)
        losses  = sum(1 for p in pnls if p <= 0)
        return {
            "total":    len(pnls),
            "wins":     wins,
            "losses":   losses,
            "avg_pnl":  sum(pnls) / len(pnls) if pnls else 0.0,
            "win_rate": wins / len(pnls) * 100 if pnls else 0.0,
            "max_pnl":  max(pnls) if pnls else 0.0,
            "min_pnl":  min(pnls) if pnls else 0.0,
        }

    # ── 백업 ──────────────────────────────────────────────────

    def backup_db(self) -> None:
        """data/backup/YYYYMMDD_trade_data.db 날짜별 백업.

        WAL 체크포인트 후 파일 복사로 일관성 보장.
        """
        try:
            backup_dir = os.path.join(os.path.dirname(self._db_path), "backup")
            os.makedirs(backup_dir, exist_ok=True)
            today = datetime.date.today().strftime("%Y%m%d")
            dst   = os.path.join(backup_dir, f"{today}_trade_data.db")
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            shutil.copy2(self._db_path, dst)
            logger.info(f"[DB] 백업 완료: {dst}")
        except Exception as e:
            logger.exception(f"[DB] backup_db 실패: {e}")

    # ── 종료 ──────────────────────────────────────────────────

    def close(self) -> None:
        """DB 연결 종료."""
        try:
            self._conn.close()
            logger.info("[DB] 연결 종료")
        except Exception:
            pass

    # ── 경로 정보 ─────────────────────────────────────────────

    @property
    def db_path(self) -> str:
        return self._db_path
