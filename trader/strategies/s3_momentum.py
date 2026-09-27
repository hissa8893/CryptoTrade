"""S3 — Time-series momentum with volatility-scaled sizing.

Long while BOTH the short (30d) and long (90d) returns are > 0 AND close > SMA(200);
evaluated daily; exit (next open) as soon as that is no longer true.
Sizing: each position targets an equal share of a whole-book volatility budget:
    weight = (target_vol / N_assets) / realized_vol(vol_lookback, annualized 365)
(dividing by N assumes the coins move together - conservative for crypto). The weight
can only SHRINK a position below the risk-engine size, never lever it up. Open positions
are rebalanced toward the target only when it moves by more than rebalance_threshold
(default 20%), to avoid fee churn.
Strength (for ranking): long-lookback return per unit of volatility.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from trader import indicators as ind
from trader.config import S3Config
from trader.strategies.base import EntrySignal, ExitSignal, Strategy, SymbolData, _f


class S3Momentum(Strategy):
    name = "S3"
    rebalances = True

    def __init__(self, cfg: S3Config | None = None):
        self.cfg = cfg or S3Config()
        self.rebalance_threshold = self.cfg.rebalance_threshold

    def params(self) -> dict:
        return self.cfg.model_dump()

    def warmup_bars(self) -> int:
        c = self.cfg
        return max(c.trend_sma, c.long_lookback, c.short_lookback, c.vol_lookback) + 1

    def prepare(self, df: pd.DataFrame) -> dict[str, np.ndarray]:
        c = self.cfg
        return {
            "s3_rs": ind.period_return(df["close"], c.short_lookback).to_numpy(),
            "s3_rl": ind.period_return(df["close"], c.long_lookback).to_numpy(),
            "s3_sma": ind.sma(df["close"], c.trend_sma).to_numpy(),
            "s3_vol": ind.realized_vol(df["close"], c.vol_lookback, c.annualization).to_numpy(),
        }

    def _on(self, sd: SymbolData, i: int) -> bool | None:
        rs, rl, sma, close = sd.ind["s3_rs"][i], sd.ind["s3_rl"][i], sd.ind["s3_sma"][i], sd.close[i]
        if not (np.isfinite(rs) and np.isfinite(rl) and np.isfinite(sma)):
            return None
        return bool(rs > 0 and rl > 0 and close > sma)

    def _snap(self, sd: SymbolData, i: int) -> dict:
        return {"close": _f(sd.close[i]), "ret_short": _f(sd.ind["s3_rs"][i]), "ret_long": _f(sd.ind["s3_rl"][i]),
                "sma": _f(sd.ind["s3_sma"][i]), "vol": _f(sd.ind["s3_vol"][i])}

    def entry(self, sd: SymbolData, i: int) -> EntrySignal | None:
        vol = sd.ind["s3_vol"][i]
        if self._on(sd, i) and np.isfinite(vol) and vol > 0:
            return EntrySignal(sd.symbol, self.name, float(sd.ind["s3_rl"][i] / vol), self._snap(sd, i))
        return None

    def exit(self, sd: SymbolData, i: int, highest_high: float) -> ExitSignal | None:
        if self._on(sd, i) is False:
            return ExitSignal(sd.symbol, self.name, "momentum_off", self._snap(sd, i))
        return None

    def target_weight(self, sd: SymbolData, i: int, n_assets: int) -> float | None:
        vol = sd.ind["s3_vol"][i]
        if not (np.isfinite(vol) and vol > 0):
            return None
        return float(min(1.0, (self.cfg.target_vol / max(1, n_assets)) / vol))
