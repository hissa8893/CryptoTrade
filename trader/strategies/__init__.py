"""Strategy registry."""

from __future__ import annotations

from trader.config import AppConfig
from trader.strategies.base import EntrySignal, ExitSignal, Strategy, SymbolData
from trader.strategies.s1_donchian import S1Donchian
from trader.strategies.s2_supertrend import S2Supertrend
from trader.strategies.s3_momentum import S3Momentum


def build_strategy(name: str, cfg: AppConfig) -> Strategy:
    name = name.upper()
    if name == "S1":
        return S1Donchian(cfg.strategies.s1)
    if name == "S2":
        return S2Supertrend(cfg.strategies.s2)
    if name == "S3":
        return S3Momentum(cfg.strategies.s3)
    raise ValueError(f"unknown strategy {name!r} (available: {', '.join(AVAILABLE)})")


AVAILABLE = ["S1", "S2", "S3"]

__all__ = ["Strategy", "SymbolData", "EntrySignal", "ExitSignal", "build_strategy", "AVAILABLE"]
