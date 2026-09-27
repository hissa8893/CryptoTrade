"""Event-driven, bar-by-bar engine. The SAME code runs backtests and live paper trading;
only the data feed differs (live mode calls `step()` once per newly closed day, with
state restored from the database).

Order of operations for each UTC day t:
  1. fill orders queued at the close of t-1, at the OPEN of t (+ slippage, + fees)
  2. resting stops (set at the close of t-1) trigger during t: gap -> open, else stop price
  3. mark to market at the CLOSE of t
  4. risk bookkeeping at the close (peak, drawdown breaker, daily loss cap)
  5. update each open position: highest high, strategy trailing stop -> stop only moves up
  6. exit signals (close of t) -> sell orders for the open of t+1
  7. entry signals (close of t), ranked by strength -> RiskManager -> buy orders for t+1
  8. equity snapshot
Decisions at t read only data up to and including bar t.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from trader import indicators as ind
from trader.broker import CostModel, Fill, Order, SimBroker, Trade
from trader.config import RiskConfig
from trader.risk import RiskEvent, RiskManager
from trader.strategies.base import Strategy, SymbolData


@dataclass
class Journal:
    """Everything the engine decided/did, ready to be written to the DB."""

    signals: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    events: list[RiskEvent] = field(default_factory=list)
    equity: list[dict] = field(default_factory=list)

    def for_date(self, d: str) -> dict:
        """Decisions made at the close of day d, as they were at decision time
        (fill outcomes excluded: they happen on d+1). Used by the look-ahead proof."""
        return {
            "signals": [s for s in self.signals if s["bar_date"] == d],
            "decisions": [x for x in self.decisions if x["bar_date"] == d],
            "orders": [{k: getattr(o, k) for k in ORDER_DECISION_FIELDS} for o in self.orders if o.created_date == d],
            "equity": [e for e in self.equity if e["bar_date"] == d],
        }


ORDER_DECISION_FIELDS = ("id", "symbol", "strategy", "side", "qty", "reason", "created_date", "stop_distance",
                         "signal_ref", "decision_ref")


class Engine:
    def __init__(
        self,
        strategies: list[Strategy],
        frames: dict[str, pd.DataFrame],
        *,
        costs: CostModel,
        risk_cfg: RiskConfig,
        starting_equity: float,
        regime_symbol: str | None,
        start: str | None = None,
        tradable: set[str] | None = None,
    ):
        if not strategies:
            raise ValueError("need at least one strategy")
        self.strategies = {s.name: s for s in strategies}
        self.tradable = set(tradable) if tradable is not None else set(frames)
        self.costs = costs
        self.risk_cfg = risk_cfg
        self.starting_equity = starting_equity
        self.sd: dict[str, SymbolData] = {}
        self.risk_atr: dict[str, np.ndarray] = {}
        for sym, df in frames.items():
            sd = SymbolData.from_frame(sym, df)
            for strat in strategies:
                sd.ind.update(strat.prepare(df))
            self.sd[sym] = sd
            self.risk_atr[sym] = ind.atr(df["high"], df["low"], df["close"], risk_cfg.atr_period).to_numpy()
        self.regime_symbol = regime_symbol
        if risk_cfg.regime_filter_enabled:
            if regime_symbol not in frames:
                raise ValueError(f"regime filter needs {regime_symbol} data")
            self.regime_sma = ind.sma(frames[regime_symbol]["close"], risk_cfg.regime_sma).to_numpy()
        all_dates = sorted(set().union(*(sd.dates for sd in self.sd.values())))
        self.dates = [d for d in all_dates if start is None or d >= start]
        self.broker = SimBroker(cash=starting_equity, costs=costs)
        self.risk = RiskManager(risk_cfg, starting_equity)
        self.journal = Journal()
        self.last_date: str | None = None
        self.last_close: dict[str, float] = {}
        self._sig_ref = 1
        self._dec_ref = 1
        self._journal_order_ids: set[int] = set()

    # -------------------------------------------------------------------------------------
    def run(self, until: str | None = None, after_step: Callable[[str, "Engine"], None] | None = None) -> Journal:
        for d in self.dates:
            if until is not None and d > until:
                break
            if self.last_date is not None and d <= self.last_date:
                continue
            self.step(d)
            if after_step:
                after_step(d, self)
        return self.journal

    def regime_ok(self, d: str) -> bool | None:
        if not self.risk_cfg.regime_filter_enabled:
            return True
        sd = self.sd[self.regime_symbol]
        i = sd.index.get(d)
        if i is None:
            return None
        sma = self.regime_sma[i]
        if not math.isfinite(sma):
            return None
        return bool(sd.close[i] > sma)

    def _signal(self, d: str, symbol: str, strategy: str, kind: str, strength: float | None, indicators: dict) -> int:
        ref = self._sig_ref
        self._sig_ref += 1
        self.journal.signals.append({"ref": ref, "bar_date": d, "symbol": symbol, "strategy": strategy,
                                     "signal": kind, "strength": strength, "indicators": indicators})
        return ref

    def _decision(self, d: str, signal_ref: int, result: str, reason: str, action: str, qty: float | None,
                  check: str) -> int:
        ref = self._dec_ref
        self._dec_ref += 1
        self.journal.decisions.append({"ref": ref, "signal_ref": signal_ref, "bar_date": d, "risk_result": result,
                                       "risk_reason": reason, "final_action": action, "final_qty": qty, "check": check})
        return ref

    def _event(self, e: RiskEvent) -> None:
        self.journal.events.append(e)

    def _record_trades(self, d: str, trades: list[Trade]) -> None:
        for t in trades:
            self.journal.trades.append(t)
            for e in self.risk.on_trade_closed(t.strategy, t.pnl, d):
                self._event(e)

    # -------------------------------------------------------------------------------------
    def step(self, d: str) -> None:
        if self.last_date is not None and d <= self.last_date:
            raise ValueError(f"day {d} already processed (last {self.last_date})")
        today = {sym: sd.index[d] for sym, sd in self.sd.items() if d in sd.index}
        b = self.broker

        # 1. fills at the open
        opens = {sym: self.sd[sym].open[i] for sym, i in today.items()}
        done, fills, trades = b.fill_at_open(d, opens)
        # orders queued before a resume are not in this session's journal yet
        self.journal.orders.extend(o for o in done if o.id not in self._journal_order_ids)
        self._journal_order_ids.update(o.id for o in done)
        self.journal.fills.extend(fills)
        for f in fills:
            if f.note:
                self._event(RiskEvent(d, "fill_shrunk", "info", f"{f.symbol}: {f.note}.", f.symbol, f.strategy))
        self._record_trades(d, trades)

        # 2. resting stops during the bar
        bars = {sym: (self.sd[sym].open[i], self.sd[sym].high[i], self.sd[sym].low[i], self.sd[sym].close[i])
                for sym, i in today.items()}
        sfills, strades = b.check_stops(d, bars)
        self.journal.fills.extend(sfills)
        for f in sfills:
            if f.reason == "stop_gap":
                self._event(RiskEvent(d, "stop_gap", "warn", f"{f.symbol} {f.note}.", f.symbol, f.strategy,
                                      {"open": f.raw_px}))
        self._record_trades(d, strades)

        # 3. mark to market at the close
        for sym, i in today.items():
            self.last_close[sym] = float(self.sd[sym].close[i])
        equity = b.equity(self.last_close)

        # 4. end-of-day risk bookkeeping
        for e in self.risk.on_close(d, equity, len(b.positions)):
            self._event(e)

        # 5. update open positions: highest high + trailing stop (only up)
        for pos in b.positions.values():
            i = today.get(pos.symbol)
            if i is None:
                continue
            sd = self.sd[pos.symbol]
            pos.bars_held += 1
            pos.highest_high = max(pos.highest_high, float(sd.high[i]))
            trail = self.strategies[pos.strategy].trailing_stop(sd, i, pos.highest_high)
            pos.stop = self.risk.ratchet_stop(pos.stop, trail)

        # 6. exits (close-based) -> sell at next open
        for pos in list(b.positions.values()):
            i = today.get(pos.symbol)
            if i is None or b.has_pending(pos.strategy, pos.symbol, "sell"):
                continue
            sig = self.strategies[pos.strategy].exit(self.sd[pos.symbol], i, pos.highest_high)
            if sig is None:
                continue
            sref = self._signal(d, pos.symbol, pos.strategy, "exit", None, sig.indicators)
            dref = self._decision(d, sref, "pass", f"Exit signal: {sig.reason}.", "sell", pos.qty, "exit")
            b.queue(symbol=pos.symbol, strategy=pos.strategy, side="sell", qty=pos.qty, reason=sig.reason,
                    created_date=d, signal_ref=sref, decision_ref=dref)

        regime = self.regime_ok(d)
        # coins that exist as of today (a coin listed later must not shrink today's vol budget)
        n_live = sum(1 for s in self.tradable if self.sd[s].dates[0] <= d)
        reserved_cash = 0.0
        added_risk = 0.0
        open_risk_now = b.open_risk(self.last_close)

        # 6b. volatility-target rebalances of open positions (S3), only if the target moved > threshold
        for pos in list(b.positions.values()):
            strat = self.strategies[pos.strategy]
            i = today.get(pos.symbol)
            if not strat.rebalances or i is None or b.has_pending(pos.strategy, pos.symbol, "sell"):
                continue
            sd = self.sd[pos.symbol]
            w = strat.target_weight(sd, i, n_live)
            dist = self.risk.initial_stop_distance(float(self.risk_atr[pos.symbol][i]))
            if w is None or dist is None:
                continue
            price = float(sd.close[i])
            target = min(w * equity / price, equity * self.risk_cfg.risk_per_trade / dist,
                         equity * self.risk_cfg.max_position_pct / price)
            change = (target - pos.qty) / pos.qty
            if abs(change) <= strat.rebalance_threshold:
                continue
            snap = {"target_qty": target, "current_qty": pos.qty, "change": change, "weight": w}
            sref = self._signal(d, pos.symbol, pos.strategy, "rebalance", None, snap)
            if target < pos.qty:
                dref = self._decision(d, sref, "pass", f"Trim {abs(change) * 100:.0f}% toward the volatility target.",
                                      "sell", pos.qty - target, "rebalance")
                b.queue(symbol=pos.symbol, strategy=pos.strategy, side="sell", qty=pos.qty - target,
                        reason="rebalance_down", created_date=d, signal_ref=sref, decision_ref=dref)
                continue
            unit_cost = price * (1 + self.costs.slippage(pos.symbol)) * (1 + self.costs.fee_rate)
            dec = self.risk.evaluate_add(
                date=d, strategy=pos.strategy, symbol=pos.symbol, price=price, add_qty=target - pos.qty,
                stop=pos.stop, equity=equity, max_affordable_qty=max(0.0, (b.cash - reserved_cash) / unit_cost),
                open_risk=open_risk_now + added_risk, regime_ok=regime)
            dref = self._decision(d, sref, dec.result, dec.reason, "buy" if dec.result != "blocked" else "none",
                                  dec.qty or None, dec.check)
            if dec.result == "blocked":
                self._event(RiskEvent(d, f"add_blocked_{dec.check}", "info",
                                      f"{pos.strategy} {pos.symbol} rebalance add blocked: {dec.reason}",
                                      pos.symbol, pos.strategy, {"signal_ref": sref}))
                continue
            b.queue(symbol=pos.symbol, strategy=pos.strategy, side="buy", qty=dec.qty, reason="rebalance_up",
                    created_date=d, signal_ref=sref, decision_ref=dref)
            reserved_cash += dec.qty * unit_cost
            added_risk += dec.qty * dec.stop_distance

        # 7. entries, strongest first
        candidates = []
        for sname, strat in self.strategies.items():
            for sym, i in today.items():
                if sym not in self.tradable or (sname, sym) in b.positions or b.has_pending(sname, sym, "buy"):
                    continue
                sig = strat.entry(self.sd[sym], i)
                if sig is not None:
                    candidates.append((sig, i))
        candidates.sort(key=lambda c: (-c[0].strength, c[0].strategy, c[0].symbol))
        accepted = 0
        for sig, i in candidates:
            sym, sname = sig.symbol, sig.strategy
            price = float(self.sd[sym].close[i])
            unit_cost = price * (1 + self.costs.slippage(sym)) * (1 + self.costs.fee_rate)
            affordable = max(0.0, (b.cash - reserved_cash) / unit_cost)
            w = self.strategies[sname].target_weight(self.sd[sym], i, n_live)
            dec = self.risk.evaluate_entry(
                date=d, strategy=sname, symbol=sym, price=price, atr=float(self.risk_atr[sym][i]), equity=equity,
                max_affordable_qty=affordable, open_risk=open_risk_now + added_risk,
                open_slots=self.risk_cfg.max_positions - len(b.positions) - accepted,
                regime_ok=regime, filled_bar=bool(self.sd[sym].filled[i]),
                strategy_cap_qty=(w * equity / price) if w is not None else None,
            )
            sref = self._signal(d, sym, sname, "enter", sig.strength, sig.indicators)
            action = "buy" if dec.result != "blocked" else "none"
            dref = self._decision(d, sref, dec.result, dec.reason, action, dec.qty or None, dec.check)
            if dec.result == "blocked":
                self._event(RiskEvent(d, f"entry_blocked_{dec.check}", "info", f"{sname} {sym} entry blocked: {dec.reason}",
                                      sym, sname, {"signal_ref": sref, "sizing": dec.sizing}))
                continue
            if dec.result == "shrunk":
                self._event(RiskEvent(d, "entry_shrunk", "info", f"{sname} {sym} entry shrunk: {dec.reason}",
                                      sym, sname, {"signal_ref": sref, "sizing": dec.sizing}))
            b.queue(symbol=sym, strategy=sname, side="buy", qty=dec.qty, reason="entry", created_date=d,
                    stop_distance=dec.stop_distance, signal_ref=sref, decision_ref=dref)
            reserved_cash += dec.qty * unit_cost
            added_risk += dec.qty * dec.stop_distance
            accepted += 1

        # 8. snapshot
        s = self.risk.state
        self.journal.equity.append({
            "bar_date": d, "equity": equity, "cash": b.cash, "positions_value": b.positions_value(self.last_close),
            "open_risk": b.open_risk(self.last_close), "peak_equity": s.peak_equity,
            "drawdown_pct": 1 - equity / s.peak_equity if s.peak_equity > 0 else 0.0,
            "positions": len(b.positions),
        })
        new_orders = [o for o in b.pending if o.created_date == d]
        self.journal.orders.extend(new_orders)
        self._journal_order_ids.update(o.id for o in new_orders)
        self.last_date = d

    # -- state (resume / crash recovery) ----------------------------------------------------
    def state_dict(self) -> dict:
        return {
            "broker": self.broker.to_dict(),
            "risk": self.risk.to_dict(),
            "last_date": self.last_date,
            "last_close": dict(self.last_close),
            "sig_ref": self._sig_ref,
            "dec_ref": self._dec_ref,
        }

    def load_state(self, st: dict) -> None:
        self.broker = SimBroker.from_dict(st["broker"], self.costs)
        self.risk = RiskManager.from_dict(self.risk_cfg, st["risk"])
        self.last_date = st["last_date"]
        self.last_close = dict(st["last_close"])
        self._sig_ref = st["sig_ref"]
        self._dec_ref = st["dec_ref"]
