"""RiskManager: deterministic, final authority over every entry.

Nothing (strategy, optional LLM) can create a trade, enlarge its size, loosen a
stop, or bypass a check. Entry checks run in this order; the first failing one
is the reason recorded:

  1. market regime       no new longs while BTC close < BTC SMA(regime_sma)
  2. stops               initial stop = entry - initial_stop_atr_mult x ATR(atr_period)
  3. position size       min(risk-based, strategy cap, max_position_pct of equity, cash)
  4. portfolio heat      total open risk <= max_portfolio_heat x equity (shrink or skip)
  5. max positions       across all strategies
  6. daily loss cap      equity down >= cap in a day -> no new entries decided that day
                         (they would fill the next day)
  7. drawdown breaker    >= breaker below peak -> block until back within release of peak
  8. losing-streak       N consecutive losers on a strategy -> pause it for M days
Every block/shrink produces a RiskEvent with a plain-English message.
Stops only ever move up.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date as Date
from datetime import timedelta

from trader.config import RiskConfig

MIN_NOTIONAL = 10.0  # USD; smaller entries are skipped as noise


@dataclass
class RiskEvent:
    date: str
    type: str
    severity: str  # info | warn | urgent
    message: str
    symbol: str | None = None
    strategy: str | None = None
    details: dict = field(default_factory=dict)


@dataclass
class EntryDecision:
    result: str  # pass | shrunk | blocked
    qty: float
    stop_distance: float
    reason: str  # plain English
    check: str  # which check decided (regime, stops, size, heat, max_positions, daily_loss, breaker, cooldown, ok)
    sizing: dict = field(default_factory=dict)


@dataclass
class RiskState:
    peak_equity: float
    prev_equity: float
    breaker_active: bool = False
    daily_loss_block_date: str | None = None
    streaks: dict[str, int] = field(default_factory=dict)
    cooldown_until: dict[str, str] = field(default_factory=dict)


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


class RiskManager:
    def __init__(self, cfg: RiskConfig, starting_equity: float, state: RiskState | None = None):
        self.cfg = cfg
        self.state = state or RiskState(peak_equity=starting_equity, prev_equity=starting_equity)

    # -- end-of-day bookkeeping ---------------------------------------------------------
    def on_close(self, date: str, equity: float) -> list[RiskEvent]:
        s, c, ev = self.state, self.cfg, []
        change = equity / s.prev_equity - 1 if s.prev_equity > 0 else 0.0
        if change <= -c.daily_loss_cap:
            s.daily_loss_block_date = date
            ev.append(RiskEvent(date, "daily_loss_cap", "urgent",
                                f"Equity fell {_pct(-change)} today (cap {_pct(c.daily_loss_cap)}): "
                                "no new entries will be opened tomorrow.",
                                details={"change": change, "equity": equity, "prev_equity": s.prev_equity}))
        s.peak_equity = max(s.peak_equity, equity)
        dd = 1 - equity / s.peak_equity if s.peak_equity > 0 else 0.0
        if not s.breaker_active and dd >= c.drawdown_breaker:
            s.breaker_active = True
            ev.append(RiskEvent(date, "circuit_breaker_on", "urgent",
                                f"Circuit breaker ON: equity is {_pct(dd)} below its peak (limit {_pct(c.drawdown_breaker)}). "
                                f"New entries blocked until back within {_pct(c.drawdown_release)} of the peak; "
                                "open positions keep their stops.",
                                details={"drawdown": dd, "equity": equity, "peak": s.peak_equity}))
        elif s.breaker_active and dd <= c.drawdown_release:
            s.breaker_active = False
            ev.append(RiskEvent(date, "circuit_breaker_off", "info",
                                f"Circuit breaker OFF: drawdown recovered to {_pct(dd)} (release at {_pct(c.drawdown_release)}). "
                                "New entries allowed again.",
                                details={"drawdown": dd, "equity": equity, "peak": s.peak_equity}))
        s.prev_equity = equity
        return ev

    def on_trade_closed(self, strategy: str, pnl: float, date: str) -> list[RiskEvent]:
        s, c = self.state, self.cfg
        s.streaks[strategy] = s.streaks.get(strategy, 0) + 1 if pnl < 0 else 0
        if s.streaks[strategy] >= c.losing_streak_limit:
            until = (Date.fromisoformat(date) + timedelta(days=c.cooldown_days)).isoformat()
            s.cooldown_until[strategy] = until
            s.streaks[strategy] = 0
            return [RiskEvent(date, "losing_streak_cooldown", "warn",
                              f"{strategy} lost {c.losing_streak_limit} trades in a row: its new entries are paused "
                              f"for {c.cooldown_days} days (until {until}).", strategy=strategy,
                              details={"until": until})]
        return []

    # -- stops ------------------------------------------------------------------------------
    def initial_stop_distance(self, atr: float) -> float | None:
        if atr is None or not math.isfinite(atr) or atr <= 0:
            return None
        return self.cfg.initial_stop_atr_mult * atr

    @staticmethod
    def ratchet_stop(current: float, proposed: float | None) -> float:
        """Effective stop = max(current (>= initial), strategy trailing stop). Never moves down."""
        if proposed is None or not math.isfinite(proposed):
            return float(current)
        return float(max(current, proposed))

    # -- entries ----------------------------------------------------------------------------
    def evaluate_entry(
        self,
        *,
        date: str,
        strategy: str,
        symbol: str,
        price: float,
        atr: float,
        equity: float,
        max_affordable_qty: float,
        open_risk: float,
        open_slots: int,
        regime_ok: bool | None,
        strategy_cap_qty: float | None = None,
        filled_bar: bool = False,
    ) -> EntryDecision:
        c, s = self.cfg, self.state

        def blocked(check: str, reason: str, stop_distance: float = 0.0, sizing: dict | None = None) -> EntryDecision:
            return EntryDecision("blocked", 0.0, stop_distance, reason, check, sizing or {})

        if filled_bar:
            return blocked("data", f"{symbol} bar is a filled data gap (no real trading that day); no entry.")
        # 1. regime
        if c.regime_filter_enabled:
            if regime_ok is None:
                return blocked("regime", f"Market regime unknown ({c.regime_asset} SMA{c.regime_sma} not available yet); no new longs.")
            if not regime_ok:
                return blocked("regime", f"{c.regime_asset} is below its {c.regime_sma}-day average (bear regime); no new longs.")
        # 2. stops
        dist = self.initial_stop_distance(atr)
        if dist is None or price <= dist:
            return blocked("stops", f"Cannot place a valid initial stop for {symbol} (ATR unavailable or too large).")
        # 3. size
        risk_qty = equity * c.risk_per_trade / dist
        caps = {"risk": risk_qty, "max_position": equity * c.max_position_pct / price, "cash": max_affordable_qty}
        if strategy_cap_qty is not None:
            caps["strategy"] = strategy_cap_qty
        binding = min(caps, key=caps.get)
        qty = max(0.0, caps[binding])
        sizing = {"risk_qty": risk_qty, "caps": caps, "binding": binding, "stop_distance": dist, "price": price}
        notes = []
        if binding != "risk":
            label = {"max_position": f"{_pct(c.max_position_pct)} of equity", "cash": "available cash",
                     "strategy": f"{strategy} volatility target"}[binding]
            notes.append(f"size capped by {label}")
        # 4. heat
        allowed = c.max_portfolio_heat * equity - open_risk
        if allowed <= 0:
            return blocked("heat", f"Portfolio heat is full: open risk {_pct(open_risk / equity)} of equity "
                                   f"(limit {_pct(c.max_portfolio_heat)}).", dist, sizing)
        if qty * dist > allowed:
            qty = allowed / dist
            notes.append(f"shrunk to fit the {_pct(c.max_portfolio_heat)} portfolio-heat limit")
            sizing["heat_limited"] = True
        # 5. max positions
        if open_slots <= 0:
            return blocked("max_positions", f"Already at the maximum of {c.max_positions} positions.", dist, sizing)
        # 6. daily loss cap
        if s.daily_loss_block_date == date:
            return blocked("daily_loss", f"Daily loss cap hit today (equity down >= {_pct(c.daily_loss_cap)}); "
                                         "no new entries until tomorrow.", dist, sizing)
        # 7. breaker
        if s.breaker_active:
            return blocked("breaker", "Circuit breaker is on (drawdown limit); new entries blocked.", dist, sizing)
        # 8. cooldown
        until = s.cooldown_until.get(strategy)
        if until and date < until:
            return blocked("cooldown", f"{strategy} is paused after a losing streak until {until}.", dist, sizing)
        if qty * price < MIN_NOTIONAL:
            return blocked("size", f"Position would be only ${qty * price:,.2f} (< ${MIN_NOTIONAL:.0f}); skipped.", dist, sizing)
        sizing["final_qty"] = qty
        if notes:
            return EntryDecision("shrunk", qty, dist, "; ".join(notes).capitalize() + ".", "size", sizing)
        return EntryDecision("pass", qty, dist, f"Risk {_pct(c.risk_per_trade)} of equity with stop {dist:,.2f} below entry.", "ok", sizing)

    # -- state ------------------------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self.state)

    @classmethod
    def from_dict(cls, cfg: RiskConfig, d: dict) -> "RiskManager":
        return cls(cfg, d["peak_equity"], RiskState(**d))
