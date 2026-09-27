"""Research: walk-forward optimisation, parameter sensitivity, Monte Carlo, regime split.

Walk-forward (headline numbers are OUT-OF-SAMPLE only):
  rolling windows of `is_years` in-sample followed by `oos_months` out-of-sample,
  stepping by `oos_months`. In each window, every parameter set on a small grid
  (default x 0.8 / 1.0 / 1.2) is backtested in-sample; the best in-sample Sharpe
  is then run, untouched, on the next out-of-sample slice. OOS slices are chained
  (each starts with the previous slice's ending equity, after paying exit costs on
  anything still open) into one stitched out-of-sample equity curve.
Every run goes through the same event-driven engine as a normal backtest, with the
price history truncated at the end of the window being evaluated.
"""

from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass, field
from datetime import date as Date

import numpy as np
import pandas as pd
from dateutil.relativedelta import relativedelta

from trader import indicators as ind
from trader.backtest import eval_start, make_engine
from trader.broker import CostModel, Trade
from trader.config import AppConfig
from trader.metrics import Metrics, compute_metrics, drawdown_stats, red_flags
from trader.strategies import build_strategy

# parameters varied per strategy (the rest stay at their configured values)
GRID_PARAMS = {
    "S1": ["entry_lookback", "exit_lookback", "chandelier_mult"],
    "S2": ["atr_period", "multiplier"],
    "S3": ["short_lookback", "long_lookback", "target_vol"],
}
# the two parameters mapped in each sensitivity heatmap
HEATMAP_PARAMS = {"S1": ("entry_lookback", "chandelier_mult"), "S2": ("atr_period", "multiplier"),
                  "S3": ("short_lookback", "long_lookback")}


def _strat_key(name: str) -> str:
    return name.lower()


def with_params(cfg: AppConfig, strategy: str, params: dict) -> AppConfig:
    key = _strat_key(strategy)
    cur = getattr(cfg.strategies, key)
    new_s = cur.model_copy(update=params)
    type(cur).model_validate(new_s.model_dump())  # stay inside the validated bounds
    return cfg.model_copy(update={"strategies": cfg.strategies.model_copy(update={key: new_s})}, deep=True)


def _scaled(value, factor):
    if isinstance(value, int) and not isinstance(value, bool):
        return max(2, int(round(value * factor)))
    return round(value * factor, 4)


def param_grid(cfg: AppConfig, strategy: str, factors=(0.8, 1.0, 1.2)) -> list[dict]:
    base = getattr(cfg.strategies, _strat_key(strategy))
    names = GRID_PARAMS[strategy]
    axes = [sorted({_scaled(getattr(base, n), f) for f in factors}) for n in names]
    grid = [dict(zip(names, combo)) for combo in itertools.product(*axes)]
    if strategy == "S3":  # the short lookback must stay shorter than the long one
        grid = [g for g in grid if g["short_lookback"] < g["long_lookback"]]
    return grid


def default_params(cfg: AppConfig, strategy: str) -> dict:
    base = getattr(cfg.strategies, _strat_key(strategy))
    return {n: getattr(base, n) for n in GRID_PARAMS[strategy]}


# ---------------------------------------------------------------------------------- runs
@dataclass
class SliceRun:
    equity: pd.Series
    trades: list[Trade]
    end_equity_net: float  # after paying exit costs on positions still open at the end
    exposure_days: int
    journal_equity: list[dict]
    risk_state: dict | None = None  # RiskManager state at the end (peak, breaker, streaks...)


def run_slice(cfg: AppConfig, frames: dict[str, pd.DataFrame], strategies: list[str], params: dict[str, dict],
              symbols: list[str], start: str, end: str, starting_equity: float,
              costs: CostModel | None = None, risk_state: dict | None = None) -> SliceRun:
    """One engine run over [start, end]. `risk_state` carries the RiskManager's memory (peak equity,
    breaker, losing streaks, cooldowns) over from the previous slice, as it would persist in live trading."""
    for s, p in params.items():
        cfg = with_params(cfg, s, p)
    costs = costs or CostModel.from_config(cfg.costs)
    eng, _, _, _ = make_engine(cfg, frames, strategies, symbols=symbols, start=start, end=end, costs=costs,
                               starting_equity=starting_equity)
    if risk_state:
        from trader.risk import RiskManager

        eng.risk = RiskManager.from_dict(cfg.risk, dict(risk_state, prev_equity=starting_equity))
    j = eng.run()
    if not j.equity:
        idx = pd.DatetimeIndex([pd.Timestamp(start, tz="UTC")])
        return SliceRun(pd.Series([starting_equity], index=idx), [], starting_equity, 0, [])
    eq = pd.Series({pd.Timestamp(e["bar_date"], tz="UTC"): e["equity"] for e in j.equity}).sort_index()
    exit_cost = sum(p.qty * eng.last_close[p.symbol] * (costs.slippage(p.symbol) + costs.fee_rate)
                    for p in eng.broker.positions.values())
    return SliceRun(eq, list(j.trades), float(eq.iloc[-1] - exit_cost),
                    sum(1 for e in j.equity if e["positions"] > 0), j.equity, eng.risk.to_dict())


@dataclass
class WFWindow:
    is_start: str
    is_end: str
    oos_start: str
    oos_end: str
    params: dict
    is_sharpe: float | None
    oos_return: float
    oos_sharpe: float | None
    oos_trades: int
    candidates: int


@dataclass
class WalkForwardResult:
    strategy: str
    symbols: list[str]
    windows: list[WFWindow]
    oos_equity: pd.Series
    oos_trades: list[Trade]
    oos_metrics: Metrics
    is_sharpe_mean: float | None
    benchmark_equity: pd.Series
    benchmark_metrics: Metrics
    flags: list[str] = field(default_factory=list)


def wf_first_date(cfg: AppConfig, frames: dict[str, pd.DataFrame], strategies: list[str], symbols: list[str]) -> str:
    """Earliest date on which every grid variant of every given strategy can trade (largest warm-up)."""
    regime_symbol = next((s for s in frames if s.split("/")[0] == cfg.risk.regime_asset), None)
    used = {s: frames[s] for s in symbols}
    if regime_symbol:
        used[regime_symbol] = frames[regime_symbol]
    firsts = []
    for strategy in strategies:
        grid = param_grid(cfg, strategy)
        widest = with_params(cfg, strategy, {k: max(g[k] for g in grid) for k in GRID_PARAMS[strategy]})
        firsts.append(eval_start(widest, used, [build_strategy(strategy, widest)], regime_symbol))
    return max(firsts)


def _add_months(d: str, months: int) -> str:
    return (Date.fromisoformat(d) + relativedelta(months=months)).isoformat()


def _prev_day(d: str) -> str:
    return (Date.fromisoformat(d) - relativedelta(days=1)).isoformat()


def walk_forward(cfg: AppConfig, frames: dict[str, pd.DataFrame], strategy: str, symbols: list[str], *,
                 is_years: int = 2, oos_months: int = 6, costs: CostModel | None = None,
                 progress=None, first: str | None = None) -> WalkForwardResult:
    from trader.backtest import buy_and_hold

    costs = costs or CostModel.from_config(cfg.costs)
    strategies = [strategy]
    grid = param_grid(cfg, strategy)
    first = first or wf_first_date(cfg, frames, [strategy], symbols)
    last = max(df.index[-1] for df in frames.values()).date().isoformat()
    windows, oos_parts, oos_trades = [], [], []
    equity = cfg.accounts.starting_equity
    risk_state = None
    is_start = first
    while True:
        oos_start = _add_months(is_start, 12 * is_years)
        if oos_start > last:
            break
        is_end = _prev_day(oos_start)
        oos_end = min(_prev_day(_add_months(oos_start, oos_months)), last)
        best, best_sharpe = None, -math.inf
        default = default_params(cfg, strategy)
        for params in grid:
            r = run_slice(cfg, frames, strategies, {strategy: params}, symbols, is_start, is_end, 10_000.0, costs)
            m = compute_metrics(r.equity, r.trades, r.exposure_days) if len(r.equity) > 1 else None
            sh = m.sharpe if m and m.sharpe is not None else -math.inf
            if sh > best_sharpe + 1e-12 or (abs(sh - best_sharpe) <= 1e-12 and params == default):
                best, best_sharpe = params, sh
        oos = run_slice(cfg, frames, strategies, {strategy: best}, symbols, oos_start, oos_end, equity, costs, risk_state)
        risk_state = oos.risk_state
        om = compute_metrics(oos.equity, oos.trades, oos.exposure_days) if len(oos.equity) > 1 else None
        windows.append(WFWindow(is_start, is_end, oos_start, oos_end, best,
                                None if best_sharpe == -math.inf else best_sharpe,
                                oos.end_equity_net / equity - 1, om.sharpe if om else None, len(oos.trades), len(grid)))
        part = oos.equity.copy()
        part.iloc[-1] = oos.end_equity_net
        oos_parts.append(part)
        oos_trades.extend(oos.trades)
        equity = oos.end_equity_net
        if progress:
            progress(len(windows), windows[-1])
        is_start = _add_months(is_start, oos_months)
    if not windows:
        raise ValueError("not enough history for one walk-forward window")
    oos_eq = pd.concat(oos_parts)
    oos_eq = oos_eq[~oos_eq.index.duplicated(keep="last")]
    # prepend the starting capital the day before the first OOS slice so returns include day 1
    day0 = oos_eq.index[0] - pd.Timedelta(days=1)
    oos_eq = pd.concat([pd.Series([cfg.accounts.starting_equity], index=[day0]), oos_eq])
    om = compute_metrics(oos_eq, oos_trades)
    bh = buy_and_hold(frames, symbols, windows[0].oos_start, windows[-1].oos_end, costs, cfg.accounts.starting_equity)
    bh = pd.concat([pd.Series([cfg.accounts.starting_equity], index=[day0]), bh])
    shs = [w.is_sharpe for w in windows if w.is_sharpe is not None]
    is_mean = float(np.mean(shs)) if shs else None
    res = WalkForwardResult(strategy, symbols, windows, oos_eq, oos_trades, om, is_mean, bh, compute_metrics(bh, []))
    res.flags = red_flags(om, oos_sharpe=om.sharpe, is_sharpe=is_mean)
    return res


# ---------------------------------------------------------------------------------- sensitivity
@dataclass
class Sensitivity:
    strategy: str
    x_name: str
    y_name: str
    x_values: list
    y_values: list
    sharpe: list[list[float | None]]  # [y][x]
    cagr: list[list[float]]
    default: tuple


def sensitivity(cfg: AppConfig, frames: dict[str, pd.DataFrame], strategy: str, symbols: list[str],
                factors=(0.6, 0.8, 1.0, 1.2, 1.4), costs: CostModel | None = None) -> Sensitivity:
    xn, yn = HEATMAP_PARAMS[strategy]
    base = getattr(cfg.strategies, _strat_key(strategy))
    xs = sorted({_scaled(getattr(base, xn), f) for f in factors})
    ys = sorted({_scaled(getattr(base, yn), f) for f in factors})
    # one common start (widest warm-up) so every cell covers the same period
    widest = with_params(cfg, strategy, {xn: max(xs), yn: max(ys)})
    regime_symbol = next((s for s in frames if s.split("/")[0] == cfg.risk.regime_asset), None)
    start = eval_start(widest, frames, [build_strategy(strategy, widest)], regime_symbol)
    end = max(df.index[-1] for df in frames.values()).date().isoformat()
    sh, cg = [], []
    for y in ys:
        row_s, row_c = [], []
        for x in xs:
            params = {xn: x, yn: y}
            if strategy == "S3" and params.get("short_lookback", 0) >= params.get("long_lookback", 10**9):
                row_s.append(None)
                row_c.append(float("nan"))
                continue
            r = run_slice(cfg, frames, [strategy], {strategy: params}, symbols, start, end,
                          cfg.accounts.starting_equity, costs)
            m = compute_metrics(r.equity, r.trades)
            row_s.append(m.sharpe)
            row_c.append(m.cagr)
        sh.append(row_s)
        cg.append(row_c)
    return Sensitivity(strategy, xn, yn, xs, ys, sh, cg, (getattr(base, xn), getattr(base, yn)))


# ---------------------------------------------------------------------------------- Monte Carlo
@dataclass
class MonteCarlo:
    runs: int
    trades: int
    actual_max_dd: float
    median_max_dd: float
    p95_max_dd: float  # 95th percentile of drawdown size = the "5th-percentile" (bad-tail) outcome
    worst_max_dd: float
    dd_samples: list[float]


def monte_carlo(trades: list[Trade], equity_at: dict[str, float], starting_equity: float,
                runs: int = 1000, seed: int = 42) -> MonteCarlo | None:
    """Reshuffle the ORDER of trades (their % impact on equity kept) and measure max drawdown.
    Uses each trade's P&L as a fraction of equity on its entry date, compounded in random order."""
    if len(trades) < 2:
        return None
    fr = []
    for t in trades:
        e = equity_at.get(t.entry_date) or starting_equity
        fr.append(t.pnl / e)
    fr = np.asarray(fr)

    def mdd(order):
        path = starting_equity * np.cumprod(1 + fr[order])
        path = np.concatenate([[starting_equity], path])
        peak = np.maximum.accumulate(path)
        return float((1 - path / peak).max())

    rng = np.random.default_rng(seed)
    actual = mdd(np.arange(len(fr)))
    samples = [mdd(rng.permutation(len(fr))) for _ in range(runs)]
    return MonteCarlo(runs, len(fr), actual, float(np.median(samples)), float(np.percentile(samples, 95)),
                      float(np.max(samples)), samples)


# ---------------------------------------------------------------------------------- regimes
def classify_regimes(btc: pd.DataFrame, sma_n: int = 200, slope_days: int = 20) -> pd.Series:
    """Per-day market regime from BTC: bull = close above a RISING SMA200, bear = below a
    FALLING SMA200, sideways = everything else. Uses data up to each day only."""
    sma = ind.sma(btc["close"], sma_n)
    rising = sma > sma.shift(slope_days)
    falling = sma < sma.shift(slope_days)
    reg = pd.Series("sideways", index=btc.index)
    reg[(btc["close"] > sma) & rising] = "bull"
    reg[(btc["close"] < sma) & falling] = "bear"
    reg[sma.isna()] = "unknown"
    return reg


def regime_table(equity: pd.Series, benchmark: pd.Series, regimes: pd.Series) -> list[dict]:
    r = equity.pct_change().dropna()
    b = benchmark.reindex(equity.index).ffill().pct_change().dropna()
    reg = regimes.reindex(r.index)
    rows = []
    for name in ("bull", "sideways", "bear"):
        m = reg == name
        if not m.any():
            continue
        rr, bb = r[m], b[m]
        sd = rr.std(ddof=1)
        rows.append({
            "regime": name, "days": int(m.sum()),
            "strategy_return": float(np.prod(1 + rr) - 1),
            "benchmark_return": float(np.prod(1 + bb) - 1),
            "strategy_sharpe": float(rr.mean() / sd * math.sqrt(365)) if len(rr) > 1 and sd > 0 else None,
        })
    return rows


# ---------------------------------------------------------------------------------- orchestration
@dataclass
class StrategyResearch:
    name: str
    wf: WalkForwardResult
    sens: Sensitivity | None
    mc: MonteCarlo | None
    regimes: list[dict]


@dataclass
class CombinedResearch:
    strategies: list[str]
    oos_equity: pd.Series
    trades: list[Trade]
    metrics: Metrics
    mc: MonteCarlo | None
    regimes: list[dict]
    flags: list[str]


@dataclass
class ResearchResult:
    data_source: str
    symbols: list[str]
    strategies: dict[str, StrategyResearch]
    combined: CombinedResearch | None
    benchmark_equity: pd.Series
    benchmark_metrics: Metrics
    zero_cost_check: dict = field(default_factory=dict)
    # the configured defaults, never tuned to any data, over the same out-of-sample span: the fair
    # "is re-optimising worth it?" comparison (it only stays fair if defaults are never changed to fit results)
    defaults: dict[str, Metrics] = field(default_factory=dict)


def _equity_at(equity: pd.Series) -> dict[str, float]:
    return {d.date().isoformat(): float(v) for d, v in equity.items()}


def _entry_equity(equity: pd.Series) -> dict[str, float]:
    """Equity at the close BEFORE each date (what a trade entered on that date was sized from)."""
    shifted = equity.shift(1).dropna()
    return {d.date().isoformat(): float(v) for d, v in shifted.items()}


def walk_forward_combined(cfg: AppConfig, frames: dict[str, pd.DataFrame], wfs: dict[str, WalkForwardResult],
                          symbols: list[str], costs: CostModel | None = None) -> CombinedResearch:
    """All strategies in ONE shared account, each using the parameters its own walk-forward
    chose for that window (so the combined result is out-of-sample too)."""
    names = list(wfs)
    n = len(next(iter(wfs.values())).windows)
    assert all(len(w.windows) == n for w in wfs.values()), "walk-forward windows must be aligned"
    equity, parts, trades, risk_state = cfg.accounts.starting_equity, [], [], None
    for k in range(n):
        w0 = wfs[names[0]].windows[k]
        params = {s: wfs[s].windows[k].params for s in names}
        r = run_slice(cfg, frames, names, params, symbols, w0.oos_start, w0.oos_end, equity, costs, risk_state)
        risk_state = r.risk_state
        part = r.equity.copy()
        part.iloc[-1] = r.end_equity_net
        parts.append(part)
        trades.extend(r.trades)
        equity = r.end_equity_net
    eq = pd.concat(parts)
    eq = eq[~eq.index.duplicated(keep="last")]
    eq = pd.concat([pd.Series([cfg.accounts.starting_equity], index=[eq.index[0] - pd.Timedelta(days=1)]), eq])
    m = compute_metrics(eq, trades)
    return CombinedResearch(names, eq, trades, m, monte_carlo(trades, _entry_equity(eq), cfg.accounts.starting_equity),
                            [], red_flags(m))


def run_research(cfg: AppConfig, frames: dict[str, pd.DataFrame], strategies: list[str], symbols: list[str], *,
                 data_source: str, sensitivity_grid: bool = True, progress=None) -> ResearchResult:
    from trader.backtest import buy_and_hold

    say = progress or (lambda msg: None)
    btc = next((frames[s] for s in frames if s.split("/")[0] == cfg.risk.regime_asset), None)
    regimes = classify_regimes(btc) if btc is not None else None
    first = wf_first_date(cfg, frames, strategies, symbols)
    out: dict[str, StrategyResearch] = {}
    for s in strategies:
        say(f"{s}: walk-forward")
        wf = walk_forward(cfg, frames, s, symbols, first=first)
        sens = None
        if sensitivity_grid:
            say(f"{s}: sensitivity grid")
            sens = sensitivity(cfg, frames, s, symbols)
        mc = monte_carlo(wf.oos_trades, _entry_equity(wf.oos_equity), cfg.accounts.starting_equity)
        reg = regime_table(wf.oos_equity, wf.benchmark_equity, regimes) if regimes is not None else []
        out[s] = StrategyResearch(s, wf, sens, mc, reg)
    combined = None
    if len(strategies) > 1:
        say("combined portfolio: walk-forward")
        combined = walk_forward_combined(cfg, frames, {s: out[s].wf for s in strategies}, symbols)
        if regimes is not None:
            combined.regimes = regime_table(combined.oos_equity, next(iter(out.values())).wf.benchmark_equity, regimes)
    first_wf = next(iter(out.values())).wf
    # sanity: with default parameters over the same span, zero costs must beat real costs
    w0, wN = first_wf.windows[0], first_wf.windows[-1]
    s0 = strategies[0]
    with_cost = run_slice(cfg, frames, [s0], {}, symbols, w0.oos_start, wN.oos_end, cfg.accounts.starting_equity)
    no_cost = run_slice(cfg, frames, [s0], {}, symbols, w0.oos_start, wN.oos_end, cfg.accounts.starting_equity,
                        CostModel.zero())
    zc = {"strategy": s0, "with_costs": with_cost.end_equity_net, "zero_costs": no_cost.end_equity_net,
          "ok": no_cost.end_equity_net > with_cost.end_equity_net}
    say("fixed-default comparison")
    defaults = {}
    for s in strategies + (["+".join(strategies)] if len(strategies) > 1 else []):
        run = run_slice(cfg, frames, s.split("+"), {}, symbols, w0.oos_start, wN.oos_end, cfg.accounts.starting_equity)
        defaults[s] = compute_metrics(run.equity, run.trades)
    return ResearchResult(data_source, symbols, out, combined, first_wf.benchmark_equity, first_wf.benchmark_metrics, zc,
                          defaults)
