"""Deterministic SYNTHETIC daily candles for offline development and tests.

These are NOT market prices. They exist so the whole system can be exercised when
the exchange is unreachable. Every report/dashboard built on them is labelled
SYNTHETIC. The generator is a regime-switching (bull/bear/sideways) random walk.
Each random stream uses its own seeded generator, so extending the end date never
changes earlier bars (the cache stays stable day to day).
"""

from __future__ import annotations

import zlib
from datetime import date

import numpy as np
import pandas as pd

from trader.timeutil import UTC

# asset: (start date, start price, base daily vol)
SYNTHETIC_ASSETS: dict[str, tuple[str, float, float]] = {
    "BTC": ("2015-01-01", 300.0, 0.035),
    "ETH": ("2016-03-01", 10.0, 0.050),
    "SOL": ("2020-08-11", 3.0, 0.065),
    "XRP": ("2017-05-01", 0.20, 0.055),
}

# regime: (daily drift, vol multiplier)
_REGIMES = np.array([[0.0025, 0.9], [-0.0025, 1.3], [0.0, 0.7]])
_TRANSITION = np.array(
    [
        [0.985, 0.008, 0.007],
        [0.010, 0.980, 0.010],
        [0.008, 0.007, 0.985],
    ]
)


def _streams(asset: str, n: int):
    seed = zlib.crc32(asset.encode())
    children = np.random.SeedSequence(seed).spawn(5)
    gens = [np.random.default_rng(s) for s in children]
    regime_u = gens[0].random(n)
    shocks = gens[1].standard_normal(n)
    hi = np.abs(gens[2].standard_normal(n))
    lo = np.abs(gens[3].standard_normal(n))
    vol = gens[4].standard_normal(n)
    return regime_u, shocks, hi, lo, vol


def generate(asset: str, end: date) -> pd.DataFrame:
    start_s, p0, sigma = SYNTHETIC_ASSETS[asset]
    start = pd.Timestamp(start_s, tz=UTC)
    idx = pd.date_range(start, pd.Timestamp(end, tz=UTC), freq="D", name="date")
    n = len(idx)
    if n <= 0:
        raise ValueError("end before synthetic start")
    regime_u, shocks, hi, lo, volz = _streams(asset, n)
    cum = np.cumsum(_TRANSITION, axis=1)
    regime = np.empty(n, dtype=int)
    r = 2
    for i in range(n):
        r = int(np.searchsorted(cum[r], regime_u[i]))
        r = min(r, 2)
        regime[i] = r
    drift = _REGIMES[regime, 0]
    vmult = _REGIMES[regime, 1]
    rets = drift + sigma * vmult * shocks
    close = p0 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[p0], close[:-1]])
    wick = sigma * vmult * 0.5
    high = np.maximum(open_, close) * np.exp(wick * hi)
    low = np.minimum(open_, close) * np.exp(-wick * lo)
    volume = np.exp(10 + 0.5 * volz)
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "filled": False},
        index=idx,
    )
    return df
