"""Strategy interface shared by backtests and live paper trading.

A strategy only PROPOSES: entry signals (with a strength used for ranking), exit
signals, and a trailing-stop level. Sizing, stop precedence and every risk check
belong to the RiskManager, which has final authority.

`prepare()` computes causal indicator columns once per symbol. Every decision at
bar i may read arrays only at indices <= i (the look-ahead proof checks this by
re-running on data truncated at i).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class SymbolData:
    """Per-symbol numpy views used by the engine (fast, index-based)."""

    symbol: str
    dates: list[str]  # ISO dates, ascending
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    filled: np.ndarray  # True = synthetic flat gap-fill bar (never traded on)
    ind: dict[str, np.ndarray] = field(default_factory=dict)  # strategy indicators
    index: dict[str, int] = field(default_factory=dict)  # date -> row

    @classmethod
    def from_frame(cls, symbol: str, df: pd.DataFrame) -> "SymbolData":
        dates = [d.date().isoformat() for d in df.index]
        filled = df["filled"].to_numpy(dtype=bool) if "filled" in df else np.zeros(len(df), dtype=bool)
        return cls(
            symbol=symbol,
            dates=dates,
            open=df["open"].to_numpy(dtype="float64"),
            high=df["high"].to_numpy(dtype="float64"),
            low=df["low"].to_numpy(dtype="float64"),
            close=df["close"].to_numpy(dtype="float64"),
            filled=filled,
            index={d: i for i, d in enumerate(dates)},
        )


@dataclass
class EntrySignal:
    symbol: str
    strategy: str
    strength: float
    indicators: dict


@dataclass
class ExitSignal:
    symbol: str
    strategy: str
    reason: str
    indicators: dict


class Strategy(ABC):
    name: str = "?"

    @abstractmethod
    def params(self) -> dict: ...

    @abstractmethod
    def warmup_bars(self) -> int:
        """Bars needed before the strategy's indicators are valid."""

    @abstractmethod
    def prepare(self, df: pd.DataFrame) -> dict[str, np.ndarray]:
        """Causal indicator arrays aligned to df's rows."""

    @abstractmethod
    def entry(self, sd: SymbolData, i: int) -> EntrySignal | None:
        """Entry proposal at the close of bar i (caller guarantees: flat in this symbol)."""

    @abstractmethod
    def exit(self, sd: SymbolData, i: int, highest_high: float) -> ExitSignal | None:
        """Close-based exit proposal at bar i for an open position."""

    def trailing_stop(self, sd: SymbolData, i: int, highest_high: float) -> float | None:
        """Optional strategy trailing stop level after bar i (the RiskManager keeps the max)."""
        return None


def _f(x: float) -> float | None:
    """JSON-safe float for indicator snapshots."""
    return None if x is None or not np.isfinite(x) else round(float(x), 8)
