"""Performance metrics (365-day annualization, zero risk-free rate)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from trader.broker import Trade

ANN = 365


@dataclass
class Metrics:
    start: str
    end: str
    days: int
    start_equity: float
    end_equity: float
    total_return: float
    cagr: float
    max_drawdown: float
    max_dd_duration_days: int
    max_dd_peak: str | None
    max_dd_trough: str | None
    sharpe: float | None
    sortino: float | None
    calmar: float | None
    trades: int
    win_rate: float | None
    profit_factor: float | None
    avg_r: float | None
    expectancy: float | None  # average P&L per trade in USD
    expectancy_r: float | None
    exposure: float  # fraction of days with at least one open position
    fees: float
    slippage: float

    def to_dict(self) -> dict:
        return asdict(self)


def drawdown_stats(equity: pd.Series) -> tuple[float, int, str | None, str | None]:
    """Max drawdown (fraction), longest time under water (days), peak/trough dates of the max DD."""
    if equity.empty:
        return 0.0, 0, None, None
    peak = equity.cummax()
    dd = 1 - equity / peak
    trough = dd.idxmax()
    max_dd = float(dd.max())
    peak_date = equity.loc[:trough].idxmax() if max_dd > 0 else None
    # longest time from a peak until equity regains it (or until the end if it never does)
    longest, peak_ts, peak_val, underwater = 0, equity.index[0], float(equity.iloc[0]), False
    for ts, v in equity.items():
        if v >= peak_val:
            if underwater:
                longest = max(longest, (ts - peak_ts).days)
            peak_ts, peak_val, underwater = ts, float(v), False
        else:
            underwater = True
    if underwater:
        longest = max(longest, (equity.index[-1] - peak_ts).days)
    fmt = lambda t: None if t is None else pd.Timestamp(t).date().isoformat()
    return max_dd, longest, fmt(peak_date), fmt(trough) if max_dd > 0 else None


def compute_metrics(equity: pd.Series, trades: list[Trade], exposure_days: int | None = None) -> Metrics:
    """equity: daily close equity indexed by UTC date (DatetimeIndex)."""
    equity = equity.astype(float)
    rets = equity.pct_change().dropna()
    days = max(1, (equity.index[-1] - equity.index[0]).days)
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (ANN / days) - 1 if equity.iloc[-1] > 0 else -1.0
    sd = rets.std(ddof=1)
    sharpe = float(rets.mean() / sd * math.sqrt(ANN)) if len(rets) > 1 and sd > 0 else None
    downside = math.sqrt(float((np.minimum(rets, 0) ** 2).mean())) if len(rets) else 0.0
    sortino = float(rets.mean() / downside * math.sqrt(ANN)) if downside > 0 else None
    max_dd, dd_days, dd_peak, dd_trough = drawdown_stats(equity)
    calmar = float(cagr / max_dd) if max_dd > 0 else None

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    gross_loss = -sum(losses)
    return Metrics(
        start=equity.index[0].date().isoformat(), end=equity.index[-1].date().isoformat(), days=days,
        start_equity=float(equity.iloc[0]), end_equity=float(equity.iloc[-1]),
        total_return=float(total), cagr=float(cagr), max_drawdown=max_dd, max_dd_duration_days=dd_days,
        max_dd_peak=dd_peak, max_dd_trough=dd_trough, sharpe=sharpe, sortino=sortino, calmar=calmar,
        trades=len(trades), win_rate=len(wins) / len(trades) if trades else None,
        profit_factor=(sum(wins) / gross_loss) if gross_loss > 0 else (None if not wins else math.inf),
        avg_r=float(np.mean(rs)) if rs else None,
        expectancy=float(np.mean(pnls)) if pnls else None,
        expectancy_r=float(np.mean(rs)) if rs else None,
        exposure=(exposure_days / len(equity)) if exposure_days is not None and len(equity) else 0.0,
        fees=float(sum(t.fees for t in trades)), slippage=float(sum(t.slippage for t in trades)),
    )


def red_flags(m: Metrics, oos_sharpe: float | None = None, is_sharpe: float | None = None) -> list[str]:
    """Automatic warnings printed at the top of every report. Each one must be investigated."""
    flags = []
    if m.trades < 30:
        flags.append(f"Only {m.trades} trades (< 30): too few to judge the strategy statistically.")
    if m.profit_factor is not None and m.profit_factor > 3:
        flags.append(f"Profit factor {m.profit_factor:.2f} > 3: unusually high, suspect a bug or overfitting.")
    if m.sharpe is not None and m.sharpe > 3:
        flags.append(f"Sharpe {m.sharpe:.2f} > 3: unusually high, suspect a bug or look-ahead.")
    if oos_sharpe is not None and is_sharpe is not None and is_sharpe > 0 and oos_sharpe < is_sharpe / 2:
        flags.append(f"Out-of-sample Sharpe {oos_sharpe:.2f} is less than half of in-sample {is_sharpe:.2f}: likely overfit.")
    return flags
