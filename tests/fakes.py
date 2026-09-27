"""Test doubles: a fake public exchange with Bitstamp-style windowed pagination."""

from __future__ import annotations

from datetime import datetime

import zlib

import numpy as np

from trader.timeutil import MS_PER_DAY, UTC, ms


class FakeExchange:
    """Returns candles in [since, since + limit days). Windows before an asset's listing
    are EMPTY (like Bitstamp). Includes the current, still-open daily candle (like real
    exchanges do) so tests prove it is dropped."""

    def __init__(self, listings: dict[str, str], now: datetime, *, page_cap: int = 1000, fail_first: int = 0,
                 overlap_days: int = 0, markets: dict | None = None):
        self.listings = {s: ms(datetime.fromisoformat(d).replace(tzinfo=UTC)) for s, d in listings.items()}
        self.now = now
        self.page_cap = page_cap
        self.fail_first = fail_first
        self.overlap_days = overlap_days
        self.calls: list[tuple[str, int, int]] = []
        self._markets = markets or {s: {"symbol": s, "spot": True, "active": True} for s in listings}
        self.has = {"fetchOHLCV": True}

    def load_markets(self):
        return self._markets

    def candle(self, symbol: str, t: int) -> list:
        i = (t - self.listings[symbol]) // MS_PER_DAY
        base = 100.0 + (zlib.crc32(symbol.encode()) % 7) + i * 0.5
        wiggle = np.sin(i / 5.0) * 2
        o = base + wiggle
        c = base + np.sin((i + 1) / 5.0) * 2
        return [t, o, max(o, c) + 1.0, min(o, c) - 1.0, c, 1000.0 + i]

    def fetch_ohlcv(self, symbol, timeframe="1d", since=None, limit=None):
        import ccxt

        self.calls.append((symbol, since, limit))
        if self.fail_first > 0:
            self.fail_first -= 1
            raise ccxt.NetworkError("simulated network blip")
        limit = min(limit or self.page_cap, self.page_cap)
        now_ms = ms(self.now)
        current_open = now_ms - now_ms % MS_PER_DAY  # today's still-open candle
        start = max(since - self.overlap_days * MS_PER_DAY, self.listings[symbol])
        start = start - start % MS_PER_DAY if start % MS_PER_DAY else start
        end = since + limit * MS_PER_DAY
        out = []
        t = start
        while t < end and t <= current_open:
            if t >= self.listings[symbol]:
                out.append(self.candle(symbol, t))
            t += MS_PER_DAY
        return out
