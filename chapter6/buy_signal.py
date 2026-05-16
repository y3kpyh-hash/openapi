# -*- coding: utf-8 -*-
"""
BuySignalScanner — 매수 신호 스캐너

전략: "강한 섹터의 대장주를 초반 눌림목에서 잡는다"

매수 조건 6가지 (각 조건 pass/fail):
  C1. 섹터 강도 상위 3위 이내  — 섹터 거래대금합 순위 ≤ 3
  C2. 섹터 확산도 60% 이상     — 섹터 내 상승종목 비율 ≥ 60%
  C3. 대장주                   — 섹터 내 leader_score 1위 AND 등급 ≥ A (≥ 65점)
  C4. 외인 순매수 +            — 종목 외인순매수 > 0
  C5. 거래대금 증가속도 급증    — 섹터 5분 증가율 ≥ 50% 또는 종목 tv_score ≥ 70
  C6. 눌림 후 재돌파           — 체결강도 ≥ 120% AND 등락률 > 1%

신호 강도 (pass 개수 기준):
  ★★★ STRONG : 6개 모두 통과
  ★★  WATCH  : 5개 통과
  ★   CHECK  : 4개 통과
  (3개 이하: 표시 안 함)
"""

from __future__ import annotations

import math
import time
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd


# ──────────────────────────────────────────────────────────────
# 기준값 (여기서 조정)
# ──────────────────────────────────────────────────────────────

CRITERIA: dict[str, float] = {
    "sector_rank":       3,      # C1: 섹터 순위 ≤ N 위
    "diffusion_pct":     60.0,   # C2: 확산도(%) ≥
    "leader_min_score":  65.0,   # C3: leader_score ≥ (A등급 기준)
    # C4: 외인 데이터 미로드(=NaN)이면 실패, 로드된 경우 대규모 매도(-100억↓)만 실패
    "foreign_net_floor": -100.0, # C4: 외인순매수 > 이 값 (미로드 NaN은 NaN>x=False → 실패)
    "sector_vel5_min":   30.0,   # C5a: 섹터 5분 증가율(%) ≥ (초기 0% 고려해 낮춤)
    "tv_score_min":      55.0,   # C5b: tv_score ≥ 중간 이상 (C5a 대체)
    "change_pct_proxy":  3.0,    # C5c: 등락률 ≥ 이 값이면 급등 중으로 간주 (C5 대체)
    "exec_str_min":      105.0,  # C6a: 체결강도(%) ≥ (기본 100보다 조금 높으면 매수세)
    "change_pct_min":    0.3,    # C6b: 등락률(%) > (상승 중)
}

CRITERIA_LABELS = {
    "C1": f"섹터순위 ≤{int(CRITERIA['sector_rank'])}위",
    "C2": f"확산도 ≥{CRITERIA['diffusion_pct']:.0f}%",
    "C3": f"대장주(leader≥{CRITERIA['leader_min_score']:.0f})",
    "C4": f"외인 >{CRITERIA['foreign_net_floor']:.0f}억",
    "C5": f"속도≥{CRITERIA['sector_vel5_min']:.0f}% or 등락률≥{CRITERIA['change_pct_proxy']:.0f}%",
    "C6": f"체결강도≥{CRITERIA['exec_str_min']:.0f}%+등락률>{CRITERIA['change_pct_min']:.1f}%",
}

SIGNAL_COLS = [
    "종목코드", "종목명", "섹터",
    "등락률(%)", "섹터순위", "확산도(%)", "외인순매수", "체결강도(%)", "5분속도(%)",
    "C1", "C2", "C3", "C4", "C5", "C6",
    "pass_cnt", "signal",
]

MIN_PASS = 3   # 이 개수 이상 통과해야 표시


@dataclass
class _SignalRecord:
    code:          str
    name:          str
    sector:        str
    change_pct:    float
    exec_strength: float
    foreign_net:   float
    tv_score:      float
    sector_rank:   int
    diffusion:     float
    leader_score:  float
    sector_vel5:   float
    ts:            float = field(default_factory=time.time)


class BuySignalScanner:
    """
    매수 신호 스캐너 — 섹터 대장주 중 6개 조건을 평가.

    사용 방법:
        scanner = BuySignalScanner(on_signal=lambda df: print(df))
        scanner.update(
            sector_summary  = sector_analyzer.get_summary(),
            leader_scores   = sector_analyzer.get_leader_scores(),
            velocity_df     = sector_analyzer.get_velocity_summary(),
            score_df        = stock_scorer.get_scores(),
        )
        df = scanner.get_signals()   # SIGNAL_COLS DataFrame, pass_cnt 내림차순
    """

    def __init__(
        self,
        on_signal: Optional[Callable[[pd.DataFrame], None]] = None,
        min_pass:  int = MIN_PASS,
    ) -> None:
        self._lock     = threading.Lock()
        self._records: dict[str, _SignalRecord] = {}
        self._on_signal = on_signal
        self._min_pass  = min_pass

    # ── 업데이트 ──────────────────────────────────────────────

    def update(
        self,
        sector_summary: pd.DataFrame,
        leader_scores:  pd.DataFrame,
        velocity_df:    pd.DataFrame,
        score_df:       pd.DataFrame,
    ) -> None:
        """모든 입력 데이터를 받아 내부 레코드를 갱신하고 콜백을 호출."""
        if sector_summary.empty or leader_scores.empty:
            return

        # 섹터 순위 맵 (1-based)
        sector_rank: dict[str, int] = {
            row["섹터명"]: i + 1
            for i, (_, row) in enumerate(sector_summary.iterrows())
        }

        # 확산도 맵
        diffusion_map: dict[str, float] = {
            row["섹터명"]: float(row.get("확산도(%)", 0))
            for _, row in sector_summary.iterrows()
        }

        # 섹터 5분 속도 맵
        vel_map: dict[str, float] = {}
        if not velocity_df.empty:
            for _, row in velocity_df.iterrows():
                vel_map[str(row["섹터명"])] = float(row.get("5분증가율(%)", 0))

        # tv_score 맵 (StockScorer)
        tv_map: dict[str, float] = {}
        if not score_df.empty:
            for _, row in score_df.iterrows():
                tv_map[str(row["종목코드"])] = float(row.get("tv_score", 0))

        records: dict[str, _SignalRecord] = {}
        for _, row in leader_scores.iterrows():
            sector = str(row["섹터명"])
            code   = str(row["종목코드"])

            records[code] = _SignalRecord(
                code          = code,
                name          = str(row.get("종목명", "")),
                sector        = sector,
                change_pct    = float(row.get("등락률(%)", 0)),
                exec_strength = float(row.get("체결강도(%)", 100)),
                foreign_net   = float(row.get("외인순매수", math.nan)),
                tv_score      = tv_map.get(code, 0.0),
                sector_rank   = sector_rank.get(sector, 999),
                diffusion     = diffusion_map.get(sector, 0.0),
                leader_score  = float(row.get("leader_score", 0)),
                sector_vel5   = vel_map.get(sector, 0.0),
            )

        with self._lock:
            self._records = records

        self._notify()

    # ── 조회 ──────────────────────────────────────────────────

    def get_signals(self) -> pd.DataFrame:
        """신호 DataFrame 반환 (pass_cnt 내림차순, 섹터순위 오름차순)."""
        with self._lock:
            records = list(self._records.values())

        if not records:
            return pd.DataFrame(columns=SIGNAL_COLS)

        rows = []
        for r in records:
            c1 = r.sector_rank    <= CRITERIA["sector_rank"]
            c2 = r.diffusion      >= CRITERIA["diffusion_pct"]
            c3 = r.leader_score   >= CRITERIA["leader_min_score"]
            # C4: 미로드(NaN)이면 NaN>x=False로 자동 실패, 로드 시 -100억 초과면 통과
            c4 = r.foreign_net    >  CRITERIA["foreign_net_floor"]
            # C5: 섹터 속도, tv_score 퍼센타일, 또는 등락률로 급등 판단
            c5 = (r.sector_vel5   >= CRITERIA["sector_vel5_min"] or
                  r.tv_score      >= CRITERIA["tv_score_min"] or
                  r.change_pct    >= CRITERIA["change_pct_proxy"])
            # C6: 체결강도(매수세)가 평균 초과 + 상승 중
            c6 = (r.exec_strength >= CRITERIA["exec_str_min"] and
                  r.change_pct    >  CRITERIA["change_pct_min"])

            pass_cnt = sum([c1, c2, c3, c4, c5, c6])
            if pass_cnt < self._min_pass:
                continue

            if   pass_cnt >= 6: signal = "★★★"
            elif pass_cnt >= 5: signal = "★★"
            elif pass_cnt >= 4: signal = "★"
            else:               signal = "⊙"   # 관심 (3개 통과, 확인 필요)

            rows.append({
                "종목코드":    r.code,
                "종목명":      r.name,
                "섹터":        r.sector,
                "등락률(%)":   r.change_pct,
                "섹터순위":    r.sector_rank,
                "확산도(%)":   r.diffusion,
                "외인순매수":  r.foreign_net,
                "체결강도(%)": r.exec_strength,
                "5분속도(%)":  r.sector_vel5,
                "C1": c1, "C2": c2, "C3": c3,
                "C4": c4, "C5": c5, "C6": c6,
                "pass_cnt": pass_cnt,
                "signal":   signal,
            })

        if not rows:
            return pd.DataFrame(columns=SIGNAL_COLS)

        return (
            pd.DataFrame(rows, columns=SIGNAL_COLS)
            .sort_values(["pass_cnt", "섹터순위"], ascending=[False, True])
            .reset_index(drop=True)
        )

    # ── 내부 ──────────────────────────────────────────────────

    def _notify(self) -> None:
        if self._on_signal is None:
            return
        try:
            self._on_signal(self.get_signals())
        except Exception:
            pass
