# -*- coding: utf-8 -*-
"""
SectorAnalyzer — 실시간 거래대금 상위 종목 섹터별 집계기

설계 원칙
- Thread-safe: update_* 메서드는 어디서든 안전하게 호출 가능
- 불변 스냅샷: get_summary() 는 호출 시점의 복사본을 반환
- 단방향 의존: trading_product ← sector_analyzer (역방향 없음)

사용 예시
----------
    analyzer = SectorAnalyzer(on_update=lambda df: print(df))

    # opt10032 수신 시
    analyzer.update_trading(top_df)

    # 외인/기관 데이터 수신 시 (opt10059, 실시간 FID 등)
    analyzer.update_investor("005930", foreign_net=100_000, inst_net=-50_000)

    # 섹터 요약 조회
    summary = analyzer.get_summary()

    # 특정 섹터 구성종목 조회
    stocks  = analyzer.get_sector_stocks("반도체")
"""

from __future__ import annotations

import math
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd


# ──────────────────────────────────────────────────────────────
# 속도 스냅샷 (시계열 캐시용)
# ──────────────────────────────────────────────────────────────

@dataclass
class _VelocitySnap:
    ts:     float             # time.time()
    values: dict[str, int]   # sector → 거래대금합 (raw unit)


VELOCITY_COLS = [
    "섹터명", "거래대금합(억)",
    "5분증가(억)", "5분증가율(%)",
    "10분증가(억)", "10분증가율(%)",
    "속도순위",
]


def _find_snap(
    cache: list[_VelocitySnap],
    target_ts: float,
) -> Optional[_VelocitySnap]:
    """cache(시간순 오름차순)에서 target_ts 이전에 가장 가까운 스냅샷 반환."""
    result: Optional[_VelocitySnap] = None
    for snap in cache:
        if snap.ts <= target_ts:
            result = snap
        else:
            break
    return result


# ──────────────────────────────────────────────────────────────
# 내부 종목 레코드
# ──────────────────────────────────────────────────────────────

@dataclass
class StockRecord:
    code:          str
    name:          str
    sector:        str
    price:         int
    change_pct:    float   # 등락률 (%)
    trading_value: int     # 거래대금 (억원)
    market_cap:    int     # 시가총액 (억원)
    rank:          int     # 거래대금 순위
    foreign_net:   float = math.nan  # 외인 순매수 (백만원); nan = 미로드
    inst_net:      float = math.nan  # 기관 순매수 (백만원); nan = 미로드
    fin_net:       float = math.nan  # 금융투자 순매수 (백만원); nan = 미로드
    exec_strength: float = 100.0 # 체결강도 (%)


# ──────────────────────────────────────────────────────────────
# SectorAnalyzer
# ──────────────────────────────────────────────────────────────

def _pct_rank(values: list[float]) -> list[float]:
    """리스트 값을 퍼센타일 순위(0~100)로 변환 (오름차순)."""
    s = pd.Series(values)
    n = len(s)
    if n <= 1:
        return [50.0] * n
    ranked = s.rank(method="average")
    return ((ranked - 1) / (n - 1) * 100).tolist()


def _leader_grade(score: float) -> str:
    if score >= 80: return "S"
    if score >= 65: return "A"
    if score >= 50: return "B"
    if score >= 35: return "C"
    return "D"


class SectorAnalyzer:
    """실시간 거래대금 상위 종목을 섹터별로 집계."""

    LEADER_COLS = [
        "섹터명", "종목코드", "종목명",
        "leader_score", "등급",
        "거래대금(억)", "등락률(%)", "외인순매수", "체결강도(%)",
    ]

    SUMMARY_COLS = [
        "섹터명", "거래대금합(억)", "평균등락률(%)",
        "확산도(%)",
        "상승종목수", "하락종목수",
        "외인순매수합(주)", "기관순매수합(주)", "금융투자순매수합(주)",
        "대장주", "종목수",
    ]

    STOCK_COLS = [
        "종목코드", "종목명", "현재가", "등락률(%)",
        "거래대금(억)", "시가총액(억)",
        "외인순매수(주)", "기관순매수(주)", "금융투자순매수(주)", "순위",
    ]

    def __init__(
        self,
        on_update: Optional[Callable[[pd.DataFrame], None]] = None,
    ) -> None:
        """
        Args:
            on_update: 데이터 변경 후 호출할 콜백.
                       인자로 get_summary() DataFrame을 전달.
        """
        self._lock     = threading.Lock()
        self._vel_lock = threading.Lock()
        self._stocks:         dict[str, StockRecord]   = {}
        self._velocity_cache: deque[_VelocitySnap]     = deque(maxlen=60)
        self._on_update = on_update

    # ── 업데이트 ──────────────────────────────────────────────

    def update_trading(self, df: pd.DataFrame) -> None:
        """
        opt10032 거래대금 상위 DataFrame으로 전체 종목 데이터 갱신.

        필수 컬럼: 종목코드, 업종, 등락률, 거래대금
        선택 컬럼: 종목명, 현재가, 시가총액
        """
        if df.empty:
            return

        required = {"종목코드", "업종", "등락률", "거래대금"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"DataFrame 누락 컬럼: {missing}")

        with self._lock:
            # 기존 외인/기관/금융투자 데이터 보존 (update_investor 로 쌓인 값)
            saved: dict[str, tuple] = {
                code: (r.foreign_net, r.inst_net, r.fin_net)
                for code, r in self._stocks.items()
            }
            self._stocks.clear()

            for rank, (_, row) in enumerate(df.iterrows(), start=1):
                code = str(row.get("종목코드", "")).strip()
                if not code:
                    continue

                sector = str(row.get("업종", "")).strip() or "미분류"
                fn, inst, fin = saved.get(code, (math.nan, math.nan, math.nan))

                self._stocks[code] = StockRecord(
                    code          = code,
                    name          = str(row.get("종목명", "")).lstrip("★").strip(),
                    sector        = sector,
                    price         = _to_int(row.get("현재가", 0)),
                    change_pct    = _to_float(row.get("등락률", 0.0)),
                    trading_value = _to_int(row.get("거래대금", 0)) // 100,  # 백만원 → 억원
                    market_cap    = _to_int(row.get("시가총액", 0)),          # 이미 억원
                    rank          = rank,
                    foreign_net   = fn,
                    inst_net      = inst,
                    fin_net       = fin,
                )

            # 속도 스냅샷: 락 안에서 섹터합 계산
            snap_vals: dict[str, int] = {}
            for r in self._stocks.values():
                snap_vals[r.sector] = snap_vals.get(r.sector, 0) + r.trading_value

        snap = _VelocitySnap(ts=time.time(), values=snap_vals)
        with self._vel_lock:
            self._velocity_cache.append(snap)

        self._notify()

    def update_investor(
        self,
        code: str,
        foreign_net: float,          # 백만원
        inst_net: float,             # 백만원
        fin_net: float = math.nan,   # 금융투자 백만원
    ) -> None:
        """
        개별 종목 외인/기관/금융투자 순매수 갱신 (단위: 백만원).
        opt10059(금액 기준), 실시간 FID 등 어디서든 호출 가능.
        """
        with self._lock:
            if code in self._stocks:
                self._stocks[code].foreign_net = foreign_net
                self._stocks[code].inst_net    = inst_net
                if not math.isnan(fin_net):
                    self._stocks[code].fin_net = fin_net
            # 아직 trading 데이터가 없으면 무시 (나중에 update_trading 이 덮어씀)

        self._notify()

    def update_realtime_price(
        self,
        code: str,
        price: int,
        change_pct: float,
    ) -> None:
        """
        실시간 주식체결(FID 10/12) 수신 시 현재가·등락률 갱신.
        거래대금·섹터는 변경하지 않음.
        """
        with self._lock:
            if code in self._stocks:
                self._stocks[code].price      = price
                self._stocks[code].change_pct = change_pct

        # 가격 갱신은 notify 생략 (빈도가 너무 높음)
        # 필요하면 self._notify() 활성화

    def update_exec_strength(self, code: str, exec_strength: float) -> None:
        """실시간 체결강도 갱신 (leader_score 계산에 사용)."""
        with self._lock:
            if code in self._stocks:
                self._stocks[code].exec_strength = exec_strength

    def clear(self) -> None:
        """전체 초기화"""
        with self._lock:
            self._stocks.clear()

    # ── 조회 ──────────────────────────────────────────────────

    def get_summary(self, min_stocks: int = 1) -> pd.DataFrame:
        """
        섹터별 집계 DataFrame 반환 (거래대금합 내림차순).

        Args:
            min_stocks: 이 수 이상 종목이 있는 섹터만 포함
        """
        with self._lock:
            stocks = list(self._stocks.values())

        if not stocks:
            return pd.DataFrame(columns=self.SUMMARY_COLS)

        # 섹터별 그룹화
        groups: dict[str, list[StockRecord]] = {}
        for s in stocks:
            groups.setdefault(s.sector, []).append(s)

        rows = []
        for sector, members in groups.items():
            if len(members) < min_stocks:
                continue

            total_value = sum(m.trading_value for m in members)
            avg_pct     = float(np.mean([m.change_pct for m in members]))
            up_cnt      = sum(1 for m in members if m.change_pct > 0)
            dn_cnt      = sum(1 for m in members if m.change_pct < 0)
            foreign_sum = sum(m.foreign_net for m in members if not math.isnan(m.foreign_net))
            inst_sum    = sum(m.inst_net    for m in members if not math.isnan(m.inst_net))
            fin_sum     = sum(m.fin_net     for m in members if not math.isnan(m.fin_net))
            leader      = max(members, key=lambda m: m.trading_value)
            diffusion   = round(up_cnt / len(members) * 100, 1)

            rows.append({
                "섹터명":              sector,
                "거래대금합(억)":       total_value,
                "평균등락률(%)":        round(avg_pct, 2),
                "확산도(%)":            diffusion,
                "상승종목수":           up_cnt,
                "하락종목수":           dn_cnt,
                "외인순매수합(주)":     foreign_sum,
                "기관순매수합(주)":     inst_sum,
                "금융투자순매수합(주)": fin_sum,
                "대장주":              leader.name,
                "종목수":              len(members),
            })

        if not rows:
            return pd.DataFrame(columns=self.SUMMARY_COLS)

        return (
            pd.DataFrame(rows, columns=self.SUMMARY_COLS)
            .sort_values("거래대금합(억)", ascending=False)
            .reset_index(drop=True)
        )

    def get_leader_scores(self) -> pd.DataFrame:
        """
        섹터별 진짜 대장주 탐지 (섹터 내 상대 점수 기준).

        leader_score = 거래대금순위(35%) + 외인순매수(25%) + 체결강도(20%) + 등락률(20%)
        각 요소는 섹터 내 퍼센타일(0~100) 또는 절대 척도로 정규화.

        Returns:
            LEADER_COLS DataFrame, leader_score 내림차순 정렬
        """
        with self._lock:
            stocks = list(self._stocks.values())

        if not stocks:
            return pd.DataFrame(columns=self.LEADER_COLS)

        # 섹터별 그룹화
        groups: dict[str, list[StockRecord]] = {}
        for s in stocks:
            groups.setdefault(s.sector, []).append(s)

        rows = []
        for sector, members in groups.items():
            if not members:
                continue

            # 섹터 내 퍼센타일 점수
            tv_scores = _pct_rank([m.trading_value for m in members])
            fn_scores = _pct_rank([m.foreign_net   for m in members])

            best_member:  StockRecord | None = None
            best_score = -1.0

            for i, m in enumerate(members):
                # 체결강도: 0~200% → 0~100 (절대)
                es = max(0.0, min(100.0, m.exec_strength / 2.0))
                # 등락률: ±10% 선형 (절대)
                cp = max(0.0, min(100.0, (m.change_pct + 10.0) / 20.0 * 100.0))

                score = (
                    tv_scores[i] * 0.35 +
                    fn_scores[i] * 0.25 +
                    es           * 0.20 +
                    cp           * 0.20
                )
                if score > best_score:
                    best_score  = score
                    best_member = m

            if best_member is None:
                continue

            rows.append({
                "섹터명":       sector,
                "종목코드":     best_member.code,
                "종목명":       best_member.name,
                "leader_score": round(best_score, 1),
                "등급":         _leader_grade(best_score),
                "거래대금(억)":  best_member.trading_value,
                "등락률(%)":    best_member.change_pct,
                "외인순매수":   best_member.foreign_net,
                "체결강도(%)":  best_member.exec_strength,
            })

        if not rows:
            return pd.DataFrame(columns=self.LEADER_COLS)

        return (
            pd.DataFrame(rows, columns=self.LEADER_COLS)
            .sort_values("leader_score", ascending=False)
            .reset_index(drop=True)
        )

    def get_velocity_summary(
        self,
        window5_sec:  int = 300,
        window10_sec: int = 600,
    ) -> pd.DataFrame:
        """
        섹터별 5분/10분 거래대금 증가속도 DataFrame 반환 (속도순위 오름차순).

        Args:
            window5_sec:  5분 창 (기본 300초)
            window10_sec: 10분 창 (기본 600초)

        Returns:
            컬럼: VELOCITY_COLS
              섹터명, 거래대금합(억),
              5분증가(억), 5분증가율(%),
              10분증가(억), 10분증가율(%),
              속도순위
        """
        with self._vel_lock:
            cache = list(self._velocity_cache)   # 시간순 오름차순 복사

        if not cache:
            return pd.DataFrame(columns=VELOCITY_COLS)

        latest   = cache[-1]
        now_ts   = latest.ts
        snap5    = _find_snap(cache, now_ts - window5_sec)
        snap10   = _find_snap(cache, now_ts - window10_sec)

        rows = []
        for sector, cur_val in latest.values.items():
            val5  = snap5.values.get(sector,  0) if snap5  else 0
            val10 = snap10.values.get(sector, 0) if snap10 else 0

            delta5  = cur_val - val5
            delta10 = cur_val - val10
            rate5   = round(delta5  / val5  * 100, 1) if val5  > 0 else 0.0
            rate10  = round(delta10 / val10 * 100, 1) if val10 > 0 else 0.0

            # 거래대금합: raw → 억 단위 표시용
            rows.append({
                "섹터명":        sector,
                "거래대금합(억)": cur_val,
                "5분증가(억)":    delta5,
                "5분증가율(%)":   rate5,
                "10분증가(억)":   delta10,
                "10분증가율(%)":  rate10,
            })

        if not rows:
            return pd.DataFrame(columns=VELOCITY_COLS)

        df = (
            pd.DataFrame(rows)
            .sort_values("5분증가(억)", ascending=False)
            .reset_index(drop=True)
        )
        df["속도순위"] = range(1, len(df) + 1)
        return df[VELOCITY_COLS]

    def get_sector_stocks(self, sector: str) -> pd.DataFrame:
        """특정 섹터 구성종목 상세 DataFrame (거래대금 내림차순)"""
        with self._lock:
            members = [r for r in self._stocks.values() if r.sector == sector]

        if not members:
            return pd.DataFrame(columns=self.STOCK_COLS)

        members.sort(key=lambda m: m.trading_value, reverse=True)
        return pd.DataFrame(
            [
                {
                    "종목코드":      m.code,
                    "종목명":        m.name,
                    "현재가":        m.price,
                    "등락률(%)":     m.change_pct,
                    "거래대금(억)":   m.trading_value,
                    "시가총액(억)":   m.market_cap,
                    "외인순매수(주)":     m.foreign_net,
                    "기관순매수(주)":     m.inst_net,
                    "금융투자순매수(주)": m.fin_net,
                    "순위":              m.rank,
                }
                for m in members
            ],
            columns=self.STOCK_COLS,
        )

    def get_leader_code(self, sector: str) -> Optional[str]:
        """섹터 대장주 종목코드 반환 (없으면 None)"""
        with self._lock:
            members = [r for r in self._stocks.values() if r.sector == sector]
        if not members:
            return None
        return max(members, key=lambda m: m.trading_value).code

    def top_sectors(self, n: int = 5) -> list[str]:
        """거래대금합 상위 N개 섹터명 반환"""
        df = self.get_summary()
        return df["섹터명"].head(n).tolist()

    # ── 프로퍼티 ──────────────────────────────────────────────

    @property
    def sector_list(self) -> list[str]:
        """현재 데이터의 섹터 목록 (거래대금합 내림차순)"""
        with self._lock:
            val: dict[str, int] = {}
            for r in self._stocks.values():
                val[r.sector] = val.get(r.sector, 0) + r.trading_value
        return sorted(val, key=lambda s: -val[s])

    @property
    def stock_count(self) -> int:
        with self._lock:
            return len(self._stocks)

    @property
    def sector_count(self) -> int:
        with self._lock:
            return len({r.sector for r in self._stocks.values()})

    # ── 내부 ──────────────────────────────────────────────────

    def _notify(self) -> None:
        if self._on_update is None:
            return
        try:
            self._on_update(self.get_summary())
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────

def _to_int(v) -> int:
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _to_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
