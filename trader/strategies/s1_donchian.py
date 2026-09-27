"""S1 — Donchian breakout with a chandelier trailing stop.

Entry (at the close of bar i, filled next open):
    close_i > highest high of the PRIOR `entry_lookback` bars (bar i excluded).
Exit (close-based, filled next open):
    close_i < lowest low of the prior `exit_lookback` bars, OR
    close_i < chandelier = highest high since entry - chandelier_mult x ATR(atr_period).
The chandelier level is also offered as a trailing stop to the RiskManager.
Strength (for ranking): breakout distance above the channel in ATRs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from trader import indicators as ind
from trader.config import S1Config
from trader.strategies.base import EntrySignal, ExitSignal, Strategy, SymbolData, _f


class S1Donchian(Strategy):
    name = "S1"

    def __init__(self, cfg: S1Config | None = None):
        self.cfg = cfg or S1Config()

    def params(self) -> dict:
        return self.cfg.model_dump()

    def warmup_bars(self) -> int:
        return max(self.cfg.entry_lookback, self.cfg.exit_lookback, self.cfg.atr_period) + 1

    def prepare(self, df: pd.DataFrame) -> dict[str, np.ndarray]:
        c = self.cfg
        return {
            "s1_dc_high": ind.donchian_high(df["high"], c.entry_lookback).to_numpy(),
            "s1_dc_low": ind.donchian_low(df["low"], c.exit_lookback).to_numpy(),
            "s1_atr": ind.atr(df["high"], df["low"], df["close"], c.atr_period).to_numpy(),
        }

    def entry(self, sd: SymbolData, i: int) -> EntrySignal | None:
        hi, a, close = sd.ind["s1_dc_high"][i], sd.ind["s1_atr"][i], sd.close[i]
        if not (np.isfinite(hi) and np.isfinite(a) and a > 0):
            return None
        if close > hi:
            return EntrySignal(
                sd.symbol, self.name, float((close - hi) / a),
                {"close": _f(close), "donchian_high": _f(hi), "atr": _f(a)},
            )
        return None

    def chandelier(self, sd: SymbolData, i: int, highest_high: float) -> float | None:
        a = sd.ind["s1_atr"][i]
        if not np.isfinite(a):
            return None
        return float(highest_high - self.cfg.chandelier_mult * a)

    def exit(self, sd: SymbolData, i: int, highest_high: float) -> ExitSignal | None:
        close, lo = sd.close[i], sd.ind["s1_dc_low"][i]
        ch = self.chandelier(sd, i, highest_high)
        snap = {"close": _f(close), "donchian_low": _f(lo), "chandelier": _f(ch), "highest_high": _f(highest_high)}
        if np.isfinite(lo) and close < lo:
            return ExitSignal(sd.symbol, self.name, "donchian_exit", snap)
        if ch is not None and close < ch:
            return ExitSignal(sd.symbol, self.name, "chandelier_exit", snap)
        return None

    def trailing_stop(self, sd: SymbolData, i: int, highest_high: float) -> float | None:
        return self.chandelier(sd, i, highest_high)
