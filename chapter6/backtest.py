"""
돌고래 전략 백테스터 (FinanceDataReader 기반)

전략 요약:
  - 블랙선 = 전일 고가 (익절 목표)
  - 초록선 = 전일 종가 (신호등 경계)
  - 빨간선 = 전일 시가 (위험 경계)
  - 🟢 초록불 (현재가 ≥ 초록선): 적극 매수
  - 🟡 노란불 (빨간선 ≤ 현재가 < 초록선): 신중
  - 🔴 빨간불 (현재가 < 빨간선): 매매 금지 / 보유 시 즉시 청산

진입: 전일 신호 발생 → 당일 시가 매수 (look-ahead bias 방지)
청산: 블랙선 도달, 빨간불 발생, stop_loss_pct 이하, 기간 종료
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Optional

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

try:
    import FinanceDataReader as fdr
except ImportError:
    raise ImportError("pip install finance-datareader 를 실행해주세요.")

warnings.filterwarnings("ignore", category=FutureWarning)

plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False


# ──────────────────────────────────────────────
# Trade 레코드
# ──────────────────────────────────────────────

@dataclass
class Trade:
    entry_date: pd.Timestamp
    entry_price: float
    quantity: int
    exit_date: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0

    def close(self, exit_date, exit_price, reason, cost_pct):
        self.exit_date  = exit_date
        self.exit_price = exit_price
        self.exit_reason = reason
        gross = (exit_price - self.entry_price) * self.quantity
        cost  = (self.entry_price + exit_price) * self.quantity * cost_pct / 100
        self.pnl     = gross - cost
        self.pnl_pct = (exit_price / self.entry_price - 1) * 100 - cost_pct


# ──────────────────────────────────────────────
# 백테스터
# ──────────────────────────────────────────────

@dataclass
class DolphineBacktester:
    stock_code: str
    start_date: str
    end_date: str

    # 전략 파라미터
    ma_length: int   = 5
    ma_condition: str = "이상"          # "이상" | "이하"
    stop_loss_pct: float = -2.0         # 음수 (예: -2.0 → -2%)
    transaction_cost_pct: float = 0.165 # 편도 0.165% (세금 0.2% 포함 시 약 0.33%)

    # 자금 관리
    initial_capital: float = 10_000_000
    buy_amount: float      = 1_000_000  # 1회 매수 금액

    # 청산 조건 활성화
    use_black_line_exit: bool = True  # 블랙선 익절
    use_green_line_exit: bool = True  # 초록선 이탈 청산
    use_red_light_exit:  bool = True  # 빨간불 즉시 청산

    # 내부 상태
    _df: pd.DataFrame = field(default_factory=pd.DataFrame, repr=False)
    _trades: list[Trade] = field(default_factory=list, repr=False)
    _equity: list[float] = field(default_factory=list, repr=False)

    # ── 데이터 로드 ──────────────────────────────

    def _load_data(self) -> None:
        raw = fdr.DataReader(self.stock_code, self.start_date, self.end_date)
        if raw.empty:
            raise ValueError(f"데이터 없음: {self.stock_code} ({self.start_date}~{self.end_date})")

        df = raw[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
        df.index = pd.to_datetime(df.index)
        df.sort_index(inplace=True)

        # 전일 OHLC (신호등 기준선)
        df['Prev_High']  = df['High'].shift(1)   # 블랙선
        df['Prev_Close'] = df['Close'].shift(1)  # 초록선
        df['Prev_Open']  = df['Open'].shift(1)   # 빨간선

        # 이동평균
        df['MA'] = df['Close'].rolling(window=self.ma_length).mean()

        df.dropna(inplace=True)
        self._df = df

    # ── 신호등 판정 ──────────────────────────────

    def _signal_zone(self, price: float, prev_close: float, prev_open: float) -> str:
        if price >= prev_close:
            return "🟢"
        elif price >= prev_open:
            return "🟡"
        return "🔴"

    # ── MA 조건 ──────────────────────────────────

    def _ma_condition_met(self, price: float, ma: float) -> bool:
        if self.ma_condition == "이상":
            return price >= ma
        return price <= ma

    # ── 시뮬레이션 ────────────────────────────────

    def run(self) -> list[Trade]:
        self._load_data()
        df = self._df
        trades: list[Trade] = []
        cash    = self.initial_capital
        holding: Optional[Trade] = None

        equity_curve = []

        for i in range(1, len(df)):
            prev = df.iloc[i - 1]
            today = df.iloc[i]
            date  = today.name

            today_open  = today['Open']
            today_high  = today['High']
            today_low   = today['Low']
            today_close = today['Close']

            black_line = today['Prev_High']   # 전일 고가 (익절)
            green_line = today['Prev_Close']  # 전일 종가 (초록선)
            red_line   = today['Prev_Open']   # 전일 시가 (빨간선)

            # ── 보유 중 청산 로직 ──────────────────
            if holding is not None:
                exit_price  = None
                exit_reason = ""

                # 1. 빨간불 청산 (당일 시가 기준)
                if self.use_red_light_exit:
                    zone = self._signal_zone(today_open, green_line, red_line)
                    if zone == "🔴":
                        exit_price  = today_open
                        exit_reason = "🔴 빨간불 청산"

                # 2. stop_loss: 당일 저가가 손절선 이하
                if exit_price is None:
                    stop_price = holding.entry_price * (1 + self.stop_loss_pct / 100)
                    if today_low <= stop_price:
                        exit_price  = max(today_open, stop_price)
                        exit_reason = f"🛑 -{abs(self.stop_loss_pct):.1f}% 손절"

                # 3. 블랙선 익절: 당일 고가가 블랙선 이상
                if exit_price is None and self.use_black_line_exit:
                    if today_high >= black_line:
                        exit_price  = black_line
                        exit_reason = "⭐ 블랙선 익절"

                # 4. 초록선 이탈: 당일 종가 < 초록선 (종가 기준)
                if exit_price is None and self.use_green_line_exit:
                    if today_close < green_line:
                        exit_price  = today_close
                        exit_reason = "📉 초록선 이탈"

                if exit_price is not None:
                    holding.close(date, exit_price, exit_reason, self.transaction_cost_pct)
                    cash += exit_price * holding.quantity
                    trades.append(holding)
                    holding = None

            # ── 매수 신호 판정 (전일 종가 기준) ────
            if holding is None:
                prev_zone = self._signal_zone(
                    prev['Close'],
                    prev['Prev_Close'],
                    prev['Prev_Open'],
                )
                ma_ok = self._ma_condition_met(prev['Close'], prev['MA'])

                if prev_zone == "🟢" and ma_ok:
                    qty = int(self.buy_amount // today_open)
                    cost = today_open * qty * (1 + self.transaction_cost_pct / 100)
                    if qty > 0 and cash >= cost:
                        holding = Trade(
                            entry_date  = date,
                            entry_price = today_open,
                            quantity    = qty,
                        )
                        cash -= today_open * qty

            # ── 자산 곡선 ──────────────────────────
            if holding is not None:
                portfolio_value = cash + today_close * holding.quantity
            else:
                portfolio_value = cash
            equity_curve.append(portfolio_value)

        # 기간 종료 시 보유 중이면 마지막 종가로 청산
        if holding is not None:
            last = df.iloc[-1]
            holding.close(last.name, last['Close'], "📅 기간 종료", self.transaction_cost_pct)
            trades.append(holding)

        self._trades  = trades
        self._equity  = equity_curve
        self._df_indexed_equity = pd.Series(
            equity_curve, index=df.index[1:]
        )
        return trades

    # ── 성과 지표 ─────────────────────────────────

    def _metrics(self) -> dict:
        if not self._trades:
            return {}

        total_pnl = sum(t.pnl for t in self._trades)
        total_ret = total_pnl / self.initial_capital * 100

        wins  = [t for t in self._trades if t.pnl > 0]
        loses = [t for t in self._trades if t.pnl <= 0]
        win_rate = len(wins) / len(self._trades) * 100 if self._trades else 0

        avg_win  = np.mean([t.pnl_pct for t in wins])  if wins  else 0.0
        avg_loss = np.mean([t.pnl_pct for t in loses]) if loses else 0.0
        pnl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

        eq = pd.Series(self._equity)
        running_max = eq.cummax()
        drawdown    = (eq - running_max) / running_max * 100
        mdd = drawdown.min()

        return {
            "총 거래 횟수": len(self._trades),
            "승률(%)":     round(win_rate, 2),
            "총 손익(원)":  round(total_pnl),
            "총 수익률(%)": round(total_ret, 2),
            "평균 수익(%)": round(avg_win, 2),
            "평균 손실(%)": round(avg_loss, 2),
            "손익비":       round(pnl_ratio, 2),
            "MDD(%)":       round(mdd, 2),
        }

    # ── 보고서 출력 ───────────────────────────────

    def report(self, show_chart: bool = True) -> None:
        if not self._trades:
            print("거래 없음.")
            return

        m = self._metrics()
        print("\n" + "=" * 50)
        print(f" 돌고래 전략 백테스팅 결과 [{self.stock_code}]")
        print(f" 기간: {self.start_date} ~ {self.end_date}")
        print(f" MA{self.ma_length} {self.ma_condition} / 손절 {self.stop_loss_pct}%")
        print("=" * 50)
        for k, v in m.items():
            print(f"  {k:<14}: {v:>12,}" if isinstance(v, int) else f"  {k:<14}: {v:>12}")
        print("=" * 50)

        print("\n[거래 내역]")
        header = f"{'진입일':<12} {'청산일':<12} {'진입가':>8} {'청산가':>8} {'수량':>5} {'손익(원)':>12} {'수익률':>8} 청산사유"
        print(header)
        print("-" * len(header))
        for t in self._trades:
            print(
                f"{str(t.entry_date.date()):<12} "
                f"{str(t.exit_date.date()):<12} "
                f"{t.entry_price:>8,.0f} "
                f"{t.exit_price:>8,.0f} "
                f"{t.quantity:>5} "
                f"{t.pnl:>12,.0f} "
                f"{t.pnl_pct:>7.2f}% "
                f"{t.exit_reason}"
            )

        if show_chart:
            self._plot()

    # ── 차트 ─────────────────────────────────────

    def _plot(self) -> None:
        df = self._df
        fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=False,
                                 gridspec_kw={'height_ratios': [3, 1.5, 1]})
        fig.suptitle(f"돌고래 전략 백테스팅 — {self.stock_code}", fontsize=14)

        # ── 패널1: 가격 + 거래 ──────────────────────
        ax1 = axes[0]
        ax1.plot(df.index, df['Close'], color='black', linewidth=0.8, label='종가')
        ax1.plot(df.index, df['MA'],    color='orange', linewidth=1.0,
                 linestyle='--', label=f'MA{self.ma_length}')

        for t in self._trades:
            ax1.axvline(t.entry_date, color='blue',  alpha=0.3, linewidth=0.6)
            ax1.axvline(t.exit_date,  color='red',   alpha=0.3, linewidth=0.6)
            color = 'red' if t.pnl > 0 else 'blue'
            ax1.annotate(
                '▲', xy=(t.entry_date, t.entry_price),
                color='blue', fontsize=8, ha='center',
            )
            ax1.annotate(
                '▼', xy=(t.exit_date, t.exit_price),
                color=color, fontsize=8, ha='center',
            )

        ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'{x:,.0f}'))
        ax1.legend(fontsize=8)
        ax1.set_ylabel("가격 (원)")
        ax1.grid(True, alpha=0.3)

        # ── 패널2: 자산 곡선 ─────────────────────────
        ax2 = axes[1]
        eq_series = self._df_indexed_equity
        ax2.plot(eq_series.index, eq_series.values, color='green', linewidth=1.0)
        ax2.axhline(self.initial_capital, color='gray', linestyle='--', linewidth=0.8)
        ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'{x/1e6:.1f}M'))
        ax2.set_ylabel("자산 (원)")
        ax2.set_title("자산 곡선", fontsize=9)
        ax2.grid(True, alpha=0.3)

        # ── 패널3: Drawdown ──────────────────────────
        ax3 = axes[2]
        eq      = eq_series.values
        run_max = np.maximum.accumulate(eq)
        dd      = (eq - run_max) / run_max * 100
        ax3.fill_between(eq_series.index, dd, 0, color='red', alpha=0.4)
        ax3.set_ylabel("DD (%)")
        ax3.set_title("Drawdown", fontsize=9)
        ax3.grid(True, alpha=0.3)

        plt.tight_layout()
        plt.show()


# ──────────────────────────────────────────────
# 실행 예시
# ──────────────────────────────────────────────

if __name__ == "__main__":
    bt = DolphineBacktester(
        stock_code   = "005930",   # 삼성전자
        start_date   = "2023-01-01",
        end_date     = "2024-12-31",
        ma_length    = 5,
        ma_condition = "이상",
        stop_loss_pct = -2.0,
        transaction_cost_pct = 0.165,
        initial_capital = 10_000_000,
        buy_amount      = 1_000_000,
        use_black_line_exit = True,
        use_green_line_exit = True,
        use_red_light_exit  = True,
    )

    bt.run()
    bt.report(show_chart=True)
