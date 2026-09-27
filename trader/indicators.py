"""Causal technical indicators, implemented from scratch so causality is auditable.

Every function returns a Series/DataFrame aligned to the input index where the
value at bar t depends ONLY on bars 0..t (no centered windows, no full-series
normalization, no backfill). Warm-up bars are NaN. Verified in tests against
hand-computed values and by a truncation test (value at t is identical whether
the input ends at t or continues past it).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def sma(series: pd.Series, n: int) -> pd.Series:
    """Simple moving average of the last n values (including the current bar)."""
    _check_n(n)
    return series.rolling(n, min_periods=n).mean()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """TR_t = max(H-L, |H-C_{t-1}|, |L-C_{t-1}|); the first bar has no prior close so TR_0 = H-L."""
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    tr.iloc[:1] = (high - low).iloc[:1]
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int) -> pd.Series:
    """Average True Range with Wilder smoothing.

    ATR_{n-1} = mean(TR_0..TR_{n-1});  ATR_t = (ATR_{t-1} * (n-1) + TR_t) / n.
    """
    _check_n(n)
    tr = true_range(high, low, close).to_numpy(dtype="float64")
    out = np.full(len(tr), np.nan)
    if len(tr) >= n:
        out[n - 1] = tr[:n].mean()
        for i in range(n, len(tr)):
            out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return pd.Series(out, index=high.index, name=f"atr{n}")


def donchian_high(high: pd.Series, n: int, include_current: bool = False) -> pd.Series:
    """Highest high of the prior n bars (excluding the current bar by default)."""
    _check_n(n)
    src = high if include_current else high.shift(1)
    return src.rolling(n, min_periods=n).max()


def donchian_low(low: pd.Series, n: int, include_current: bool = False) -> pd.Series:
    """Lowest low of the prior n bars (excluding the current bar by default)."""
    _check_n(n)
    src = low if include_current else low.shift(1)
    return src.rolling(n, min_periods=n).min()


def supertrend(
    high: pd.Series, low: pd.Series, close: pd.Series, atr_period: int = 10, multiplier: float = 3.0
) -> pd.DataFrame:
    """Supertrend (same band logic as TradingView's ta.supertrend).

    basic_upper = hl2 + m*ATR, basic_lower = hl2 - m*ATR.
    final_upper_t = basic_upper_t if basic_upper_t < final_upper_{t-1} or close_{t-1} > final_upper_{t-1}
                    else final_upper_{t-1}   (lower band symmetric)
    direction: +1 bullish / -1 bearish. It starts bearish on the first bar with a valid
    ATR (conservative: an entry needs a real bullish flip). Bearish -> bullish when
    close > final_upper; bullish -> bearish when close < final_lower.
    Columns: supertrend, direction, upper, lower, flip (+1 bullish flip, -1 bearish flip, 0 none).
    """
    a = atr(high, low, close, atr_period).to_numpy()
    h = high.to_numpy(dtype="float64")
    lo = low.to_numpy(dtype="float64")
    c = close.to_numpy(dtype="float64")
    n = len(c)
    hl2 = (h + lo) / 2.0
    bu = hl2 + multiplier * a
    bl = hl2 - multiplier * a
    fu = np.full(n, np.nan)
    fl = np.full(n, np.nan)
    direction = np.full(n, np.nan)
    st = np.full(n, np.nan)
    flip = np.zeros(n)
    start = atr_period - 1
    for i in range(start, n):
        if i == start:
            fu[i], fl[i], direction[i] = bu[i], bl[i], -1.0
        else:
            fu[i] = bu[i] if (bu[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
            fl[i] = bl[i] if (bl[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
            if direction[i - 1] < 0:
                direction[i] = 1.0 if c[i] > fu[i] else -1.0
            else:
                direction[i] = -1.0 if c[i] < fl[i] else 1.0
            if direction[i] != direction[i - 1]:
                flip[i] = direction[i]
        st[i] = fl[i] if direction[i] > 0 else fu[i]
    return pd.DataFrame(
        {"supertrend": st, "direction": direction, "upper": fu, "lower": fl, "flip": flip}, index=close.index
    )


def log_returns(close: pd.Series) -> pd.Series:
    return np.log(close / close.shift(1))


def realized_vol(close: pd.Series, n: int, annualization: int = 365) -> pd.Series:
    """Annualized realized volatility: sample stdev (ddof=1) of the last n daily log
    returns times sqrt(annualization). First valid at bar n."""
    _check_n(n, minimum=2)
    return log_returns(close).rolling(n, min_periods=n).std(ddof=1) * math.sqrt(annualization)


def period_return(close: pd.Series, n: int) -> pd.Series:
    """Simple return over the last n bars: C_t / C_{t-n} - 1."""
    _check_n(n)
    return close / close.shift(n) - 1.0


def _check_n(n: int, minimum: int = 1) -> None:
    if not isinstance(n, (int, np.integer)) or n < minimum:
        raise ValueError(f"window must be an integer >= {minimum}, got {n!r}")
