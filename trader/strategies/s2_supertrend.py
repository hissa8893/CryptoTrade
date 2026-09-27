"""S2 — Supertrend with an SMA trend filter.

Entry (close of bar i, filled next open): the Supertrend(ATR atr_period, multiplier)
direction FLIPS bullish on bar i AND close_i > SMA(trend_sma).
Exit (close-based, filled next open): the direction is bearish (a bearish flip).
Strength (for ranking): how far the close is above the trend SMA.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from trader import indicators as ind
from trader.config import S2Config
from trader.strategies.base import EntrySignal, ExitSignal, Strategy, SymbolData, _f


class S2Supertrend(Strategy):
    name = "S2"

    def __init__(self, cfg: S2Config | None = None):
        self.cfg = cfg or S2Config()

    def params(self) -> dict:
        return self.cfg.model_dump()

    def warmup_bars(self) -> int:
        return max(self.cfg.trend_sma, self.cfg.atr_period) + 1

    def prepare(self, df: pd.DataFrame) -> dict[str, np.ndarray]:
        st = ind.supertrend(df["high"], df["low"], df["close"], self.cfg.atr_period, self.cfg.multiplier)
        return {
            "s2_st": st["supertrend"].to_numpy(),
            "s2_dir": st["direction"].to_numpy(),
            "s2_flip": st["flip"].to_numpy(),
            "s2_sma": ind.sma(df["close"], self.cfg.trend_sma).to_numpy(),
        }

    def entry(self, sd: SymbolData, i: int) -> EntrySignal | None:
        flip, sma, close = sd.ind["s2_flip"][i], sd.ind["s2_sma"][i], sd.close[i]
        if flip > 0 and np.isfinite(sma) and close > sma:
            return EntrySignal(sd.symbol, self.name, float(close / sma - 1),
                               {"close": _f(close), "sma": _f(sma), "supertrend": _f(sd.ind["s2_st"][i])})
        return None

    def exit(self, sd: SymbolData, i: int, highest_high: float) -> ExitSignal | None:
        if sd.ind["s2_dir"][i] < 0:
            return ExitSignal(sd.symbol, self.name, "supertrend_flip",
                              {"close": _f(sd.close[i]), "supertrend": _f(sd.ind["s2_st"][i])})
        return None
