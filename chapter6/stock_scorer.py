# -*- coding: utf-8 -*-
"""
StockScorer — 실시간 종목 강도 점수 시스템

점수 구성 요소 (각 0~100, 가중 합산 → 최종 0~100):
  거래대금 증가율  35%  — 세션 시작 대비 누적 거래대금 증가
  등락률          25%  — 절대 척도 (-10%→0, 0%→50, +10%→100)
  외인 순매수     20%  — 퍼센타일 순위
  체결강도        10%  — 절대 척도 (0%→0, 100%→50, 200%→100)
  거래량 증가율   10%  — 세션 시작 대비 누적 거래량 증가

등급:  S(≥80) / A(≥65) / B(≥50) / C(≥35) / D(<35)

사용 예시:
    scorer = StockScorer(on_update=lambda df: print(df))
    scorer.update_trading(top_df)                          # opt10032 수신
    scorer.update_realtime("005930", change_pct=2.5,
                           volume=5_000_000, exec_strength=145.0)
    scorer.update_investor("005930", foreign_net=300, inst_net=-50)
    print(scorer.get_scores())
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd


# ──────────────────────────────────────────────────────────────
# 가중치
# ──────────────────────────────────────────────────────────────

WEIGHTS: dict[str, float] = {
    "trading_value": 0.35,
    "change_pct":    0.25,
    "foreign_net":   0.20,
    "exec_strength": 0.10,
    "volume":        0.10,
}

OUTPUT_COLS = [
    "종목코드", "종목명",
    "등락률(%)", "거래대금", "거래량",
    "외인순매수", "체결강도(%)",
    "score", "등급",
    "tv_score", "cp_score", "fn_score", "es_score", "vol_score",
]


# ──────────────────────────────────────────────────────────────
# 내부 레코드
# ──────────────────────────────────────────────────────────────

@dataclass
class _StockData:
    code:               str
    name:               str   = ""
    change_pct:         float = 0.0
    trading_value:      int   = 0      # 거래대금 (원시값, opt10032 단위)
    trading_value_base: int   = 0      # 세션 시작 기준값
    volume:             int   = 0      # 누적 거래량
    volume_base:        int   = 0      # 세션 시작 기준값
    exec_strength:      float = 100.0  # 체결강도 (%)
    foreign_net:        float = 0.0    # 외인 순매수
    inst_net:           float = 0.0    # 기관 순매수
    ts:                 float = field(default_factory=time.time)


# ──────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────

def _percentile_score(series: pd.Series) -> pd.Series:
    """값 → 퍼센타일 점수 0~100 (동일값은 평균 순위)."""
    n = len(series)
    if n == 0:
        return series.copy()
    ranked = series.rank(method="average")
    return (ranked - 1) / max(n - 1, 1) * 100.0


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


def _grade(score: float) -> str:
    if score >= 80: return "S"
    if score >= 65: return "A"
    if score >= 50: return "B"
    if score >= 35: return "C"
    return "D"


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


# ──────────────────────────────────────────────────────────────
# StockScorer
# ──────────────────────────────────────────────────────────────

class StockScorer:
    """실시간 종목 강도 점수 집계기 (thread-safe)."""

    def __init__(
        self,
        on_update: Optional[Callable[[pd.DataFrame], None]] = None,
        notify_throttle_ms: int = 500,
    ) -> None:
        """
        Args:
            on_update: 점수 변경 후 호출할 콜백 (get_scores() DataFrame 전달).
            notify_throttle_ms: update_realtime 콜백 최소 간격 (ms).
        """
        self._lock          = threading.Lock()
        self._stocks:       dict[str, _StockData] = {}
        self._on_update     = on_update
        self._throttle_sec  = notify_throttle_ms / 1000.0
        self._last_notify   = 0.0

    # ── 업데이트 ──────────────────────────────────────────────

    def update_trading(self, df: pd.DataFrame) -> None:
        """
        opt10032 거래대금 상위 DataFrame 일괄 갱신.

        필수 컬럼: 종목코드
        선택 컬럼: 종목명, 등락률, 거래대금, 거래량
        """
        if df.empty:
            return
        with self._lock:
            for _, row in df.iterrows():
                code = str(row.get("종목코드", "")).strip()
                if not code:
                    continue
                tv  = _to_int(row.get("거래대금", 0))
                vol = _to_int(row.get("거래량", 0))
                if code in self._stocks:
                    d = self._stocks[code]
                    d.name       = str(row.get("종목명", d.name)).lstrip("★").strip()
                    d.change_pct = _to_float(row.get("등락률", d.change_pct))
                    d.trading_value = tv
                    d.volume        = vol
                    # 기준값은 처음 수신 시 한 번만 설정
                    if d.trading_value_base == 0 and tv > 0:
                        d.trading_value_base = tv
                    if d.volume_base == 0 and vol > 0:
                        d.volume_base = vol
                    d.ts = time.time()
                else:
                    self._stocks[code] = _StockData(
                        code               = code,
                        name               = str(row.get("종목명", "")).lstrip("★").strip(),
                        change_pct         = _to_float(row.get("등락률", 0.0)),
                        trading_value      = tv,
                        trading_value_base = tv,
                        volume             = vol,
                        volume_base        = vol,
                    )
        self._notify()

    def update_realtime(
        self,
        code: str,
        *,
        change_pct:    Optional[float] = None,
        volume:        Optional[int]   = None,
        exec_strength: Optional[float] = None,
        trading_value: Optional[int]   = None,
    ) -> None:
        """
        실시간 FID 수신 시 개별 필드 갱신.
        None 인자는 기존 값을 유지.
        """
        with self._lock:
            if code not in self._stocks:
                return
            d = self._stocks[code]
            if change_pct    is not None: d.change_pct    = change_pct
            if exec_strength is not None: d.exec_strength = exec_strength
            if volume is not None:
                d.volume = volume
                if d.volume_base == 0 and volume > 0:
                    d.volume_base = volume
            if trading_value is not None:
                d.trading_value = trading_value
                if d.trading_value_base == 0 and trading_value > 0:
                    d.trading_value_base = trading_value
            d.ts = time.time()
        self._notify(throttled=True)

    def update_investor(
        self,
        code: str,
        foreign_net: float,
        inst_net:    float,
    ) -> None:
        """외인/기관 순매수 갱신."""
        with self._lock:
            if code in self._stocks:
                self._stocks[code].foreign_net = foreign_net
                self._stocks[code].inst_net    = inst_net
        self._notify()

    def reset_baselines(self) -> None:
        """장 시작·점심 후 등 기준값을 현재값으로 리셋."""
        with self._lock:
            for d in self._stocks.values():
                d.trading_value_base = d.trading_value or d.trading_value_base
                d.volume_base        = d.volume        or d.volume_base

    def clear(self) -> None:
        """전체 초기화."""
        with self._lock:
            self._stocks.clear()

    # ── 조회 ──────────────────────────────────────────────────

    def get_scores(self) -> pd.DataFrame:
        """
        점수 DataFrame 반환 (score 내림차순).

        컬럼: OUTPUT_COLS 참조
          종목코드, 종목명, 등락률(%), 거래대금, 거래량,
          외인순매수, 체결강도(%),
          score(0~100), 등급(S/A/B/C/D),
          tv_score, cp_score, fn_score, es_score, vol_score
        """
        with self._lock:
            stocks = list(self._stocks.values())

        if not stocks:
            return pd.DataFrame(columns=OUTPUT_COLS)

        df = pd.DataFrame([{
            "code":               d.code,
            "name":               d.name,
            "change_pct":         d.change_pct,
            "trading_value":      d.trading_value,
            "trading_value_base": d.trading_value_base,
            "volume":             d.volume,
            "volume_base":        d.volume_base,
            "exec_strength":      d.exec_strength,
            "foreign_net":        d.foreign_net,
        } for d in stocks])

        # ── 1) 거래대금 증가율 (퍼센타일) ────────────────────
        safe_tv = df["trading_value_base"].replace(0, np.nan)
        tv_rate = (df["trading_value"] / safe_tv - 1.0).fillna(0.0)
        df["tv_score"] = _percentile_score(tv_rate)

        # ── 2) 등락률 (절대, ±10% 선형) ──────────────────────
        df["cp_score"] = df["change_pct"].apply(
            lambda x: _clamp((x + 10.0) / 20.0 * 100.0)
        )

        # ── 3) 외인 순매수 (퍼센타일) ────────────────────────
        df["fn_score"] = _percentile_score(df["foreign_net"])

        # ── 4) 체결강도 (절대, 0~200% → 0~100점) ─────────────
        df["es_score"] = df["exec_strength"].apply(
            lambda x: _clamp(x / 2.0)
        )

        # ── 5) 거래량 증가율 (퍼센타일) ──────────────────────
        safe_vol = df["volume_base"].replace(0, np.nan)
        vol_rate = (df["volume"] / safe_vol - 1.0).fillna(0.0)
        df["vol_score"] = _percentile_score(vol_rate)

        # ── 가중 합산 ─────────────────────────────────────────
        df["score"] = (
            df["tv_score"]  * WEIGHTS["trading_value"] +
            df["cp_score"]  * WEIGHTS["change_pct"]    +
            df["fn_score"]  * WEIGHTS["foreign_net"]   +
            df["es_score"]  * WEIGHTS["exec_strength"] +
            df["vol_score"] * WEIGHTS["volume"]
        ).round(1)

        df["등급"] = df["score"].apply(_grade)

        return (
            df.rename(columns={
                "code":          "종목코드",
                "name":          "종목명",
                "change_pct":    "등락률(%)",
                "trading_value": "거래대금",
                "volume":        "거래량",
                "foreign_net":   "외인순매수",
                "exec_strength": "체결강도(%)",
            })
            [OUTPUT_COLS]
            .sort_values("score", ascending=False)
            .reset_index(drop=True)
        )

    def get_score_map(self) -> dict[str, tuple[float, str]]:
        """code → (score, grade) 빠른 조회용 dict."""
        df = self.get_scores()
        if df.empty:
            return {}
        return {
            row["종목코드"]: (row["score"], row["등급"])
            for _, row in df.iterrows()
        }

    def get_top_n(self, n: int = 10) -> pd.DataFrame:
        """점수 상위 N개 종목."""
        return self.get_scores().head(n)

    # ── 프로퍼티 ──────────────────────────────────────────────

    @property
    def stock_count(self) -> int:
        with self._lock:
            return len(self._stocks)

    # ── 내부 ──────────────────────────────────────────────────

    def _notify(self, throttled: bool = False) -> None:
        if self._on_update is None:
            return
        now = time.time()
        if throttled and (now - self._last_notify) < self._throttle_sec:
            return
        self._last_notify = now
        try:
            self._on_update(self.get_scores())
        except Exception:
            pass
