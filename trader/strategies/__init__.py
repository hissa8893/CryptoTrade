"""Strategy registry."""

from __future__ import annotations

from trader.config import AppConfig
from trader.strategies.base import EntrySignal, ExitSignal, Strategy, SymbolData
from trader.strategies.s1_donchian import S1Donchian


def build_strategy(name: str, cfg: AppConfig) -> Strategy:
    name = name.upper()
    if name == "S1":
        return S1Donchian(cfg.strategies.s1)
    raise ValueError(f"unknown strategy {name!r} (available: S1)")


AVAILABLE = ["S1"]

__all__ = ["Strategy", "SymbolData", "EntrySignal", "ExitSignal", "build_strategy", "AVAILABLE"]
