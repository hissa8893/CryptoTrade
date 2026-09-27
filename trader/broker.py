"""SimBroker: simulated fills with fees and slippage. Never talks to an exchange.

Timing (no look-ahead):
* Orders are queued at the CLOSE of bar t and filled at the OPEN of bar t+1.
  Buys pay open x (1 + slippage); sells receive open x (1 - slippage); every fill
  pays fee_rate on its notional.
* Stops are resting orders placed at the close of bar t and live during bar t+1:
  if the open is already at/below the stop, the fill is at the OPEN (gap through
  the stop); otherwise, if the low touches the stop, the fill is at the stop.
  Slippage applies to stop fills too.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from trader.config import CostsConfig


class CostModel:
    def __init__(self, fee_rate: float, slip_major: float, slip_other: float, major_assets: list[str]):
        if min(fee_rate, slip_major, slip_other) < 0:
            raise ValueError("costs cannot be negative")
        self.fee_rate = fee_rate
        self.slip_major = slip_major
        self.slip_other = slip_other
        self.major = {a.upper() for a in major_assets}

    @classmethod
    def from_config(cls, c: CostsConfig) -> "CostModel":
        return cls(c.fee_rate, c.slippage_major, c.slippage_other, c.major_assets)

    @classmethod
    def zero(cls) -> "CostModel":
        """Research sanity check ONLY (a zero-cost run must beat the with-cost run)."""
        return cls(0.0, 0.0, 0.0, [])

    def slippage(self, symbol: str) -> float:
        return self.slip_major if symbol.split("/")[0].upper() in self.major else self.slip_other

    @property
    def is_zero(self) -> bool:
        return self.fee_rate == 0 and self.slip_major == 0 and self.slip_other == 0

    def describe(self) -> dict:
        return {"fee_rate": self.fee_rate, "slippage_major": self.slip_major, "slippage_other": self.slip_other,
                "major_assets": sorted(self.major)}


@dataclass
class Position:
    symbol: str
    strategy: str
    qty: float
    entry_px: float  # average fill price incl. slippage (changes when a rebalance adds)
    entry_date: str
    initial_stop: float
    stop: float
    highest_high: float
    entry_fee: float  # all buy fees paid so far (entry + adds)
    entry_slippage: float  # all slippage paid so far (buys + partial sells)
    trade_no: int
    entry_signal_ref: int | None = None
    bars_held: int = 0
    realized_pnl: float = 0.0  # from partial sells (rebalances), net of their fees
    sell_fees: float = 0.0  # fees of partial sells (already inside realized_pnl)
    max_qty: float = 0.0
    initial_risk_usd: float = 0.0  # qty x (entry - initial stop) at the first fill: the "1R"

    @property
    def key(self) -> tuple[str, str]:
        return (self.strategy, self.symbol)

    @property
    def initial_risk(self) -> float:
        return self.initial_risk_usd


@dataclass
class Order:
    id: int
    symbol: str
    strategy: str
    side: str  # buy | sell
    qty: float  # planned qty at decision time (never modified afterwards)
    reason: str
    created_date: str
    stop_distance: float | None = None  # buys: initial stop = fill price - stop_distance
    signal_ref: int | None = None
    decision_ref: int | None = None
    status: str = "pending"
    fill_date: str | None = None
    fill_px: float | None = None
    fee: float | None = None
    slippage: float | None = None
    filled_qty: float | None = None  # buys may shrink to available cash at the fill


@dataclass
class Trade:
    trade_no: int
    symbol: str
    strategy: str
    entry_date: str
    entry_px: float
    qty: float
    initial_stop: float
    exit_date: str
    exit_px: float
    exit_reason: str
    fees: float
    slippage: float
    pnl: float
    pnl_pct: float
    r_multiple: float | None
    bars_held: int
    entry_signal_ref: int | None = None
    exit_signal_ref: int | None = None


@dataclass
class Fill:
    date: str
    symbol: str
    strategy: str
    side: str
    qty: float
    raw_px: float
    px: float
    fee: float
    slippage: float
    reason: str
    order_id: int | None = None
    note: str = ""


MIN_QTY = 1e-10


@dataclass
class SimBroker:
    cash: float
    costs: CostModel
    positions: dict[tuple[str, str], Position] = field(default_factory=dict)
    pending: list[Order] = field(default_factory=list)
    next_order_id: int = 1
    next_trade_no: int = 1

    # -- queueing ---------------------------------------------------------------------
    def queue(self, **kw) -> Order:
        o = Order(id=self.next_order_id, **kw)
        self.next_order_id += 1
        self.pending.append(o)
        return o

    def pending_buys(self) -> list[Order]:
        return [o for o in self.pending if o.side == "buy"]

    def has_pending(self, strategy: str, symbol: str, side: str) -> bool:
        return any(o.strategy == strategy and o.symbol == symbol and o.side == side for o in self.pending)

    # -- fills at the open ------------------------------------------------------------
    def fill_at_open(self, date: str, opens: dict[str, float]) -> tuple[list[Order], list[Fill], list[Trade]]:
        """Fill every pending order whose symbol has a bar today: sells first, then buys."""
        done, fills, trades = [], [], []
        remaining = []
        for o in sorted(self.pending, key=lambda o: (o.side != "sell", o.id)):
            px_open = opens.get(o.symbol)
            if px_open is not None:
                px_open = float(px_open)
            if px_open is None:
                remaining.append(o)  # no bar for this symbol today; try next bar
                continue
            if o.side == "sell":
                pos = self.positions.get((o.strategy, o.symbol))
                if pos is None:  # already closed (e.g. stopped out); nothing to sell
                    o.status = "cancelled"
                    done.append(o)
                    continue
                if o.qty < pos.qty * (1 - 1e-9):  # rebalance trim: partial sell, position stays open
                    f, t = self._reduce(pos, date, px_open, o.qty, o.reason, o.id), None
                else:
                    f, t = self._close(pos, date, px_open, o.reason, o.id, o.signal_ref)
                o.status, o.fill_date, o.fill_px, o.fee, o.slippage = "filled", date, f.px, f.fee, f.slippage
                o.filled_qty = f.qty
                done.append(o)
                fills.append(f)
                if t is not None:
                    trades.append(t)
            else:
                f = self._open(o, date, px_open)
                done.append(o)
                if f is not None:
                    fills.append(f)
        self.pending = remaining
        return done, fills, trades

    def _open(self, o: Order, date: str, px_open: float) -> Fill | None:
        slip = self.costs.slippage(o.symbol)
        px = px_open * (1 + slip)
        max_qty = self.cash / (px * (1 + self.costs.fee_rate)) if px > 0 else 0.0
        qty = min(o.qty, max_qty)
        note = ""
        if qty < o.qty * (1 - 1e-9):
            note = f"shrunk at fill from {o.qty:.8f} to {qty:.8f} (open above estimate; cash limit)"
        if qty <= MIN_QTY:
            o.status = "cancelled"
            return None
        fee = qty * px * self.costs.fee_rate
        existing = self.positions.get((o.strategy, o.symbol))
        if existing is None and o.reason.startswith("rebalance"):
            o.status = "cancelled"  # the position it was meant to top up is gone; never open without a stop
            return None
        self.cash -= qty * px + fee
        if existing is not None:  # rebalance add: average in, stop unchanged
            existing.entry_px = (existing.qty * existing.entry_px + qty * px) / (existing.qty + qty)
            existing.qty += qty
            existing.max_qty = max(existing.max_qty, existing.qty)
            existing.entry_fee += fee
            existing.entry_slippage += qty * (px - px_open)
            o.status, o.fill_date, o.fill_px, o.fee, o.slippage, o.filled_qty = "filled", date, px, fee, qty * (px - px_open), qty
            return Fill(date, o.symbol, o.strategy, "buy", qty, px_open, px, fee, qty * (px - px_open), o.reason, o.id, note)
        if not o.stop_distance or o.stop_distance <= 0 or o.stop_distance >= px:
            self.cash += qty * px + fee  # undo: a new position must have a valid protective stop
            o.status = "cancelled"
            return None
        stop = px - o.stop_distance
        pos = Position(
            symbol=o.symbol, strategy=o.strategy, qty=qty, entry_px=px, entry_date=date,
            initial_stop=stop, stop=stop, highest_high=px_open, entry_fee=fee,
            entry_slippage=qty * (px - px_open), trade_no=self.next_trade_no, entry_signal_ref=o.signal_ref,
            max_qty=qty, initial_risk_usd=qty * (px - stop),
        )
        self.next_trade_no += 1
        self.positions[pos.key] = pos
        o.status, o.fill_date, o.fill_px, o.fee, o.slippage, o.filled_qty = "filled", date, px, fee, qty * (px - px_open), qty
        return Fill(date, o.symbol, o.strategy, "buy", qty, px_open, px, fee, qty * (px - px_open), o.reason, o.id, note)

    def _reduce(self, pos: Position, date: str, raw_px: float, qty: float, reason: str, order_id: int | None) -> Fill:
        slip = self.costs.slippage(pos.symbol)
        px = raw_px * (1 - slip)
        fee = qty * px * self.costs.fee_rate
        self.cash += qty * px - fee
        pos.realized_pnl += (px - pos.entry_px) * qty - fee
        pos.sell_fees += fee
        pos.entry_slippage += qty * (raw_px - px)
        pos.qty -= qty
        return Fill(date, pos.symbol, pos.strategy, "sell", qty, raw_px, px, fee, qty * (raw_px - px), reason, order_id)

    def _close(self, pos: Position, date: str, raw_px: float, reason: str, order_id: int | None,
               exit_signal_ref: int | None = None) -> tuple[Fill, Trade]:
        slip = self.costs.slippage(pos.symbol)
        px = raw_px * (1 - slip)
        fee = pos.qty * px * self.costs.fee_rate
        slip_cost = pos.qty * (raw_px - px)
        self.cash += pos.qty * px - fee
        del self.positions[pos.key]
        fees = pos.entry_fee + pos.sell_fees + fee
        pnl = pos.realized_pnl + (px - pos.entry_px) * pos.qty - fee - pos.entry_fee
        risk = pos.initial_risk
        qty = max(pos.max_qty, pos.qty)
        trade = Trade(
            trade_no=pos.trade_no, symbol=pos.symbol, strategy=pos.strategy, entry_date=pos.entry_date,
            entry_px=pos.entry_px, qty=qty, initial_stop=pos.initial_stop, exit_date=date, exit_px=px,
            exit_reason=reason, fees=fees, slippage=pos.entry_slippage + slip_cost, pnl=pnl,
            pnl_pct=pnl / (pos.entry_px * qty), r_multiple=(pnl / risk) if risk > 0 else None,
            bars_held=pos.bars_held, entry_signal_ref=pos.entry_signal_ref, exit_signal_ref=exit_signal_ref,
        )
        return Fill(date, pos.symbol, pos.strategy, "sell", pos.qty, raw_px, px, fee, slip_cost, reason, order_id), trade

    # -- resting stops during the bar -------------------------------------------------
    def check_stops(self, date: str, bars: dict[str, tuple[float, float, float, float]]) -> tuple[list[Fill], list[Trade]]:
        """bars: symbol -> (open, high, low, close) for today."""
        fills, trades = [], []
        for pos in list(self.positions.values()):
            bar = bars.get(pos.symbol)
            if bar is None:
                continue
            o, low = float(bar[0]), float(bar[2])
            if pos.entry_date == date:
                # entered at today's open (above the stop by construction); only the low can hit it
                if low <= pos.stop:
                    f, t = self._close(pos, date, pos.stop, "stop", None)
                    fills.append(f)
                    trades.append(t)
                continue
            if o <= pos.stop:
                f, t = self._close(pos, date, o, "stop_gap", None)
                f.note = f"opened at {o:.8g}, through the stop {pos.stop:.8g}: filled at the open"
                fills.append(f)
                trades.append(t)
            elif low <= pos.stop:
                f, t = self._close(pos, date, pos.stop, "stop", None)
                fills.append(f)
                trades.append(t)
        return fills, trades

    # -- valuation ----------------------------------------------------------------------
    def positions_value(self, closes: dict[str, float]) -> float:
        return sum(p.qty * closes.get(p.symbol, p.entry_px) for p in self.positions.values())

    def equity(self, closes: dict[str, float]) -> float:
        return self.cash + self.positions_value(closes)

    def open_risk(self, closes: dict[str, float]) -> float:
        """Sum of (price - stop) x qty: what open positions would lose if every stop hit."""
        return sum(max(0.0, closes.get(p.symbol, p.entry_px) - p.stop) * p.qty for p in self.positions.values())

    # -- state (crash recovery / resume) -------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "positions": [asdict(p) for p in self.positions.values()],
            "pending": [asdict(o) for o in self.pending],
            "next_order_id": self.next_order_id,
            "next_trade_no": self.next_trade_no,
        }

    @classmethod
    def from_dict(cls, d: dict, costs: CostModel) -> "SimBroker":
        b = cls(cash=d["cash"], costs=costs, next_order_id=d["next_order_id"], next_trade_no=d["next_trade_no"])
        for p in d["positions"]:
            pos = Position(**p)
            b.positions[pos.key] = pos
        b.pending = [Order(**o) for o in d["pending"]]
        return b


def isfinite(x) -> bool:
    return x is not None and math.isfinite(x)
